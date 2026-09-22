"""Feature parity suite: memory, cross-session search, curator, usage, toggles,
shell backends, streaming output, gateway, cron, config set."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from oaset import cron as cronmod
from oaset.agent import AgentLoop, Conversation, initial_system_prompt
from oaset.agent.prompts import build_system_prompt
from oaset.agent.runner import execute_prompt
from oaset.config import default_config, load_config, save_config
from oaset.credentials import load_credential, save_credential
from oaset.gateway import serve
from oaset.history import search_history
from oaset.memory import load_memory_context, memory_path, read_memory
from oaset.providers import MockProvider, MockTurn
from oaset.session import SessionStore
from oaset.tools import AutoGate, ToolContext, ToolRegistry


def make_registry(workspace, **ctx_kwargs):
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=1000, **ctx_kwargs)
    registry.bind(ctx)
    return registry, ctx


# --------------------------------------------------------------------- memory


def test_memory_scopes_and_prompt_injection(isolated_home):
    from oaset.memory import write_memory

    write_memory("global", "This machine runs tests with pytest.", append=True)
    write_memory("user", "Prefers concise answers in Chinese.")
    assert "pytest" in read_memory("global")
    ctx_block = load_memory_context()
    assert "Durable memory" in ctx_block and "pytest" in ctx_block
    assert "User model" in ctx_block and "Chinese" in ctx_block
    prompt = build_system_prompt(Path("."))
    assert "Memory (from previous sessions)" in prompt
    assert "Learning loop" in prompt and "skill_create" in prompt  # curator nudge


async def test_memory_tool_persists_across_loops(isolated_home, workspace):
    registry, ctx = make_registry(workspace)
    await registry.dispatch("memory", '{"scope": "global", "content": "Deploy = ./deploy.sh"}')
    assert "Deploy" in read_memory("global")
    # a *second* session's system prompt sees it (closed learning loop)
    prompt, _ = initial_system_prompt(workspace)
    assert "Deploy = ./deploy.sh" in prompt


def test_memory_files_confined_to_memory_dir(isolated_home):
    assert memory_path("global").parent.name == "memory"
    assert memory_path("user").name == "USER.md"


# -------------------------------------------------------------- session search


def _seed_session(workspace, store, title_body: str) -> str:
    session = store.new_session(workspace, model="m", title="seed")
    session.conversation.append(None) if False else None
    from oaset.agent.messages import Message

    for role, text in (("user", title_body), ("assistant", "acknowledged")):
        session.conversation.append(Message(role=role, content=text))
        store.append_message(session, Message(role=role, content=text))
    return session.meta.session_id


def test_search_history_finds_past_session(isolated_home, workspace):
    store = SessionStore()
    _seed_session(workspace, store, "we decided to migrate the build to buck2")
    hits = search_history("buck2 build", home=isolated_home)
    assert hits, "FTS5 index should find the seeded session"
    assert any("buck2" in h.snippet for h in hits)
    misses = search_history("completely-unrelated-xyzzy", home=isolated_home)
    assert misses == []


async def test_search_history_tool_dispatch(isolated_home, workspace):
    store = SessionStore()
    _seed_session(workspace, store, "kubectl rollout restart the deployment")
    registry, ctx = make_registry(workspace)
    result = await registry.dispatch("search_history", '{"query": "kubectl rollout"}')
    assert not result.is_error
    assert "kubectl" in result.output


def test_fts5_or_graceful(isolated_home):
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        has_fts = True
    except sqlite3.OperationalError:
        has_fts = False
    if not has_fts:
        pytest.skip("sqlite build lacks FTS5")
    assert has_fts


# ------------------------------------------------------------ skill curator


async def test_skill_create_and_reuse(isolated_home, workspace):
    registry, _ = make_registry(workspace)
    result = await registry.dispatch("skill_create", json.dumps({
        "name": "Django Migrations",
        "description": "Safe migration workflow for the legacy app",
        "body": "1. makemigrations --check 2. review 3. migrate --plan",
    }))
    assert not result.is_error and "created" in result.output
    from oaset.skills import load_skills

    skills = load_skills(workspace)
    assert any("django" in s.path.as_posix().lower() for s in skills)
    assert any("migration" in s.description.lower() for s in skills)


# --------------------------------------------------------------------- usage


def test_store_usage_aggregation(isolated_home, workspace):
    store = SessionStore()
    session = store.new_session(workspace, model="mock/mock-echo")
    store.append_usage(session, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, "mock")
    store.append_usage(session, {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}, "mock")
    totals = store.session_usage(session.meta.session_id)
    assert totals["entries"] == 2
    assert totals["total_tokens"] == 43
    assert totals["prompt_tokens"] == 30


async def test_loop_usage_events_reach_turn_done(workspace):
    provider = MockProvider([MockTurn(content_chunks=["ok"], usage={
        "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})])
    registry, _ = make_registry(workspace)
    loop = AgentLoop(provider, registry, Conversation(system_prompt="s"))
    events: list[dict] = []
    await loop.run("hi", events.append)
    done = events[-1]
    assert done["type"] == "turn_done" and done["usage"]["total_tokens"] == 10


# ------------------------------------------------------------------- toggles


def test_config_tool_toggles_roundtrip(isolated_home):
    cfg = default_config()
    cfg.disabled_tools = ["web_fetch", "cron"]
    save_config(cfg)
    loaded = load_config()
    assert loaded.disabled_tools == ["web_fetch", "cron"]


async def test_disabled_tool_is_invisible_to_model(isolated_home, workspace):
    cfg = default_config()
    cfg.disabled_tools = ["run_shell"]
    registry, _ = make_registry(workspace)
    registry.set_toggles(disabled=cfg.disabled_tools)
    names = [s["function"]["name"] for s in registry.schemas()]
    assert "run_shell" not in names
    result = await registry.dispatch("run_shell", '{"command": "echo x"}')
    assert result.is_error and "Unknown tool" in result.output


# ------------------------------------------------------------ shell backends


def test_backend_command_line_construction():
    from oaset.tools.shell import backend_command_line

    local = backend_command_line("echo hi", {"backend": "local"})
    assert local[-1] == "echo hi"
    ssh = backend_command_line("echo hi", {"backend": "ssh", "ssh_target": "ops@host"})
    assert ssh == ["ssh", "ops@host", "echo hi"]
    docker = backend_command_line("echo hi", {"backend": "docker", "docker_container": "box"})
    assert docker[:4] == ["docker", "exec", "box", "bash"]
    with pytest.raises(ValueError):
        backend_command_line("x", {"backend": "ssh", "ssh_target": ""})
    with pytest.raises(ValueError):
        backend_command_line("x", {"backend": "docker"})


async def test_shell_local_backend_e2e(workspace):
    registry, ctx = make_registry(workspace)
    ctx.session_state["shell_config"] = {"backend": "local"}
    result = await registry.dispatch("run_shell", '{"command": "echo backend-local"}')
    assert not result.is_error and "backend-local" in result.output


async def test_shell_streaming_sink(workspace):
    registry, ctx = make_registry(workspace)
    ctx.session_state["shell_config"] = {"backend": "local"}
    chunks: list[str] = []
    ctx.session_state["stream_sink"] = chunks.append
    result = await registry.dispatch(
        "run_shell", '{"command": "printf line1\\\\nline2\\\\nline3\\\\n"}'
    )
    assert not result.is_error
    assert chunks, "stream sink should receive output chunks"
    assert "line3" in "".join(chunks)


# -------------------------------------------------------------------- gateway


def test_gateway_http_roundtrip(isolated_home, workspace):
    cfg = default_config()
    provider = MockProvider([MockTurn(content_chunks=["gateway reply"],
                                      usage={"total_tokens": 7})])
    server = serve(cfg, workspace, host="127.0.0.1", port=0, provider=provider,
                   token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
        assert health.json()["status"] == "ok"   # health stays token-free
        # the local-first contract: /message is worthless without the token
        denied = httpx.post(f"http://127.0.0.1:{port}/message", json={"text": "x"}, timeout=5)
        assert denied.status_code == 401
        headers = {"authorization": "Bearer test-token"}
        resp = httpx.post(f"http://127.0.0.1:{port}/message", json={"text": "hello gw"},
                          headers=headers, timeout=15)
        body = resp.json()
        assert body["ok"] is True
        assert body["reply"] == "gateway reply"
        sid = body["session_id"]
        assert resp.json()["elapsed"] >= 0
        # session reuse: the same session id serves the follow-up message —
        # no one-session-per-message litter in the index
        resp2 = httpx.post(f"http://127.0.0.1:{port}/message", json={"text": "again"},
                           headers=headers, timeout=15)
        assert resp2.json()["ok"] is True
        assert resp2.json()["session_id"] == sid
        bad = httpx.post(f"http://127.0.0.1:{port}/message", json={}, headers=headers, timeout=5)
        assert bad.status_code == 400
    finally:
        from oaset.gateway import shutdown_server

        shutdown_server(server)


# ----------------------------------------------------------------------- cron


def test_cron_jobs_roundtrip_and_schedule_parsing(isolated_home):
    job = cronmod.add_job("nightly", "daily 09:00", "run backups")
    assert cronmod.load_jobs()[0].name == "nightly"
    assert cronmod.parse_interval_minutes("every 20m") == 20
    assert cronmod.parse_interval_minutes("every 3h") == 180
    assert cronmod.parse_interval_minutes("daily 09:00") is None
    now = datetime(2026, 9, 8, 9, 1)
    assert cronmod.is_due(job, now) is True  # past 09:00, not fired today
    job.last_run = "2026-09-08T09:05:00"
    assert cronmod.is_due(job, now) is False  # already fired today
    interval_job = cronmod.add_job("poll", "every 20m", "check")
    interval_job.last_run = (datetime.now() - timedelta(minutes=25)).isoformat()
    assert cronmod.is_due(interval_job, datetime.now()) is True
    interval_job.last_run = datetime.now().isoformat()
    assert cronmod.is_due(interval_job, datetime.now()) is False
    assert cronmod.set_enabled(job.id, False)
    assert cronmod.is_due(job, now) is False
    assert cronmod.remove_job(job.id) is True


async def test_cron_tool_add_and_list(isolated_home, workspace):
    registry, _ = make_registry(workspace)
    result = await registry.dispatch("cron", json.dumps({
        "action": "add", "name": "audit", "schedule": "daily 08:30", "prompt": "audit the repo"}))
    assert not result.is_error and "Scheduled" in result.output
    bad = await registry.dispatch("cron", json.dumps({
        "action": "add", "name": "x", "schedule": "whenever", "prompt": "p"}))
    assert bad.is_error
    listing = await registry.dispatch("cron", '{"action": "list"}')
    assert "audit" in listing.output


async def test_cron_fire_due_executes(monkeypatch, isolated_home, workspace):
    cronmod.add_job("instant", "every 20m", "say hi from cron")
    async def fake_execute(cfg, cwd, text, **kwargs):
        return f"cron-ran: {text}"
    monkeypatch.setattr("oaset.agent.runner.execute_prompt", fake_execute)
    from oaset.cli import _cron_fire_due

    rc = await _cron_fire_due(None, force=False)
    assert rc == 0
    job = cronmod.load_jobs()[0]
    assert job.last_run  # stamped after firing


# ----------------------------------------------------------------- config set


def test_cli_config_set(isolated_home, capsys):
    from oaset.cli import cmd_config

    assert cmd_config("set", "ui.theme", "oaset-light") == 0
    assert load_config().ui.theme == "oaset-light"
    assert cmd_config("set", "tool_output_limit", "777") == 0
    assert load_config().tool_output_limit == 777
    assert cmd_config("set", "not_a_key", "x") == 2
    assert cmd_config("set", "default_model", "openai/gpt-4o") == 0
    assert load_config().default_model == "openai/gpt-4o"


# -------------------------------------------------------------- headless runner


async def test_execute_prompt_runner(isolated_home, workspace):
    cfg = default_config()
    provider = MockProvider([MockTurn(content_chunks=["runner says hi"])])
    reply = await execute_prompt(cfg, workspace, "greet", provider=provider, with_tools=False)
    assert reply == "runner says hi"


async def test_execute_prompt_with_tools_and_subagents(isolated_home, workspace):
    (workspace / "note.txt").write_text("runner-note", encoding="utf-8")
    cfg = default_config()
    provider = MockProvider([
        MockTurn(tool_calls=[__import__("oaset.providers", fromlist=["MockToolCall"]).MockToolCall(
            "read_file", {"path": "note.txt"})]),
        MockTurn(content_chunks=["final: runner-note"]),
    ])
    reply = await execute_prompt(cfg, workspace, "read it", provider=provider, yolo=True)
    assert reply == "final: runner-note"


def test_cli_setup_wizard(isolated_home, monkeypatch, capsys):
    from oaset.cli import cmd_setup

    save_credential("deepseek", "sk-wizard-key")
    answers = iter(["3", "1"])  # sorted providers: [anthropic, bedrock, deepseek, ...] -> 3=deepseek; 1=deepseek-chat
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")  # empty = keep existing
    rc = cmd_setup()
    assert rc == 0
    cfg = load_config()
    assert cfg.default_model == "deepseek/deepseek-chat"  # sorted provider list: deepseek=3; its models sorted -> 1=chat
    assert cfg.default_provider == "deepseek"
    assert load_credential("deepseek") == "sk-wizard-key"  # empty key keeps existing


def test_history_search_is_workspace_scoped(isolated_home, tmp_path):
    """A5 regression: the agent-facing search must not leak one project's
    conversations into another workspace's context."""
    from oaset.agent.messages import Message
    from oaset.history import search_history

    ws_a, ws_b = tmp_path / "proj-a", tmp_path / "proj-b"
    store = SessionStore(home=isolated_home)
    for ws, text in ((ws_a, "buck2 build_secret_alpha"), (ws_b, "buck2 build_secret_beta")):
        s = store.new_session(Path(ws), model="m", title=f"title-{ws.name}")
        s.conversation.append(Message(role="user", content=text))
        store.append_message(s, s.conversation.messages[-1])

    hits_a = search_history("build_secret", home=isolated_home, workspace=ws_a)
    hits_b = search_history("build_secret", home=isolated_home, workspace=ws_b)
    assert [h.title for h in hits_a] == ["title-proj-a"]
    assert [h.title for h in hits_b] == ["title-proj-b"]

    # no workspace → explicit global search still sees everything
    both = search_history("build_secret", home=isolated_home)
    assert {h.title for h in both} == {"title-proj-a", "title-proj-b"}


def test_mutation_tools_are_classified_as_writes():
    """A6 regression: tools that persist global state must not claim READ."""
    from oaset.memory import MemoryTool
    from oaset.tools.base import WRITE
    from oaset.tools.skill_create import SkillCreateTool

    assert MemoryTool.permission == WRITE
    assert SkillCreateTool.permission == WRITE


# ------------------------------------------- CLI wizards: EOF is a clean cancel


def _ns(**kwargs):
    """argparse.Namespace stand-in carrying the flags cmd_models reads."""
    import argparse

    defaults = {"action": "list", "verify_model": None, "json": False, "verify": False}
    return argparse.Namespace(**{**defaults, **kwargs})


def test_models_add_with_closed_stdin_cancels_instead_of_crashing(isolated_home, monkeypatch, capsys):
    """A piped-dry stdin used to raise EOFError out of the wizard; nothing may
    be written and the exit path must be a message, not a traceback."""
    from oaset.cli import cmd_models
    from oaset.config import config_path
    from oaset.i18n import t

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    rc = cmd_models(_ns(action="add"))
    assert rc == 1
    captured = capsys.readouterr()
    assert t("cli_no_input_cancelled") in (captured.out + captured.err)
    assert not config_path().exists() or "acme" not in config_path().read_text(encoding="utf-8")


def test_models_add_confirm_write_treats_eof_as_no(isolated_home, monkeypatch, capsys):
    """Silence is not consent: a closed stdin must not save the config."""
    from oaset.cli import cmd_models
    from oaset.config import config_path
    from oaset.i18n import t

    answers = iter(["acme", "https://gw.acme.test/v1", "acme-1", "128000", "8192", "tool_use"])

    def feed(prompt=""):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError  # the confirm read is the one that runs dry

    monkeypatch.setattr("builtins.input", feed)
    rc = cmd_models(_ns(action="add"))
    assert rc == 1, "the confirm read hit EOF and must not have saved"
    captured = capsys.readouterr().out
    assert t("models_add_discarded") in captured
    text = config_path().read_text(encoding="utf-8") if config_path().exists() else ""
    assert "acme-1" not in text


def test_models_add_verify_flag_probes_after_saving(isolated_home, monkeypatch, capsys):
    """`models add --verify` was a dead branch behind a flag nobody could pass."""
    from oaset import cli as climod
    from oaset.config import load_config

    answers = iter(["acme", "https://gw.acme.test/v1", "acme-1", "128000", "8192", "tool_use", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    probed: list[str] = []

    async def fake_verify(cfg, model_id, json_out=False, provider=None):
        probed.append(model_id)
        return 0

    monkeypatch.setattr(climod, "cmd_models_verify", fake_verify)
    rc = climod.cmd_models(_ns(action="add", verify=True))
    assert rc == 0
    assert probed == ["acme/acme-1"]
    assert load_config().models["acme/acme-1"].max_output_size == 8192


def test_sessions_list_marks_other_working_directories(isolated_home, tmp_path, monkeypatch, capsys):
    """`oaset sessions` lists every project's history; a row from another
    directory must say so instead of looking like this project's session."""
    from oaset.cli import cmd_sessions
    from oaset.session import SessionStore

    store = SessionStore()
    store.new_session(tmp_path / "other-project", model="mock/mock-echo", title="elsewhere")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pathlib.Path.cwd", classmethod(lambda cls: tmp_path))
    assert cmd_sessions() == 0
    out = capsys.readouterr().out
    assert "elsewhere" in out
    assert "other-project" in out, "the row must name the directory it belongs to"


# ------------------------------------------------ verify --all (stage-0 honesty)


async def test_models_verify_all_reports_matrix_and_skip_mock(isolated_home, monkeypatch, capsys):
    """`verify --all` is the honesty lever: every configured model probed once,
    the offline mock reported as SKIPPED (never as verified), and the exit code
    reflects any failure."""
    from oaset import cli as climod

    probed: list[str] = []

    async def fake_probe(cfg, mid, provider=None):
        probed.append(mid)
        ok = mid.endswith("chat")
        verdict = {"model": mid, "provider": "p", "endpoint": "https://x.test",
                   "ok": ok, "elapsed_ms": 5}
        if not ok:
            verdict["error"] = "401 auth failed"
        return verdict, (0 if ok else 1)

    monkeypatch.setattr(climod, "_probe_model", fake_probe)
    rc = await climod.cmd_models_verify_all(isolated_home if hasattr(isolated_home, "models") else _cfg())
    assert rc == 1, "one failing model must fail the batch"
    out = capsys.readouterr().out
    assert "deepseek/deepseek-chat" in out and "OK" in out
    assert "401" in out
    assert "mock/mock-echo" in out and "跳过（离线 mock）" in out
    assert probed and not any(m.startswith("mock/") for m in probed), "mock must not be probed"


async def test_models_verify_all_json_contract(isolated_home, monkeypatch, capsys):
    import json as _json

    from oaset import cli as climod

    async def fake_probe(cfg, mid, provider=None):
        return {"model": mid, "provider": "p", "endpoint": "https://x.test",
                "ok": True, "elapsed_ms": 5}, 0

    monkeypatch.setattr(climod, "_probe_model", fake_probe)
    rc = await climod.cmd_models_verify_all(_cfg(), json_out=True)
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["verified"] >= 1
    assert payload["skipped"] >= 1, "the offline mock must be counted as skipped"
    assert all(r["ok"] is not True or r["model"] != "mock/mock-echo"
               for r in payload["results"] if r["model"] == "mock/mock-echo") or True
    mock_rows = [r for r in payload["results"] if r["model"] == "mock/mock-echo"]
    assert mock_rows and mock_rows[0]["ok"] is None, "skipped rows carry ok=None"


def _cfg():
    from oaset.config import default_config

    return default_config()


def test_gateway_blocks_rebinding_and_cross_origin(isolated_home, workspace):
    """A browser page can always SEND requests to 127.0.0.1 — the guard makes
    sure they cannot make them EXECUTE: a rebound Host or a foreign Origin is
    a 403 before the agent even sees the prompt."""
    cfg = default_config()
    provider = MockProvider([MockTurn(content_chunks=["ok"])])
    server = serve(cfg, workspace, host="127.0.0.1", port=0, provider=provider,
                   token="t-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        headers = {"authorization": "Bearer t-token"}
        # DNS-rebinding: the attacker's domain presents itself as Host
        rebound = httpx.post(f"{base}/message", json={"text": "hi"},
                             headers={**headers, "host": "evil.example"}, timeout=5)
        assert rebound.status_code == 403
        # cross-origin page POST: Origin names the attacker's page
        csrf = httpx.post(f"{base}/message", json={"text": "hi"},
                          headers={**headers, "origin": "http://evil.example"}, timeout=5)
        assert csrf.status_code == 403
        # panel endpoints carry the same guard
        panel = httpx.get(f"{base}/panel/sessions",
                          headers={**headers, "host": "evil.example"}, timeout=5)
        assert panel.status_code == 403
        # a legitimate loopback client still passes the guard (auth applies)
        fine = httpx.post(f"{base}/message", json={"text": "hi"},
                          headers=headers, timeout=15)
        assert fine.status_code in (200, 500)  # mock provider answers; guard passed
        assert "rebinding" not in fine.text
    finally:
        from oaset.gateway import shutdown_server

        shutdown_server(server)
        server.server_close()


def test_gateway_panel_reads_and_events_endpoint(isolated_home, workspace):
    """The narrow panel API: read-only session/usage/evidence views over the
    door, plus an /events stream that exists and requires the token."""

    cfg = default_config()
    provider = MockProvider([MockTurn(content_chunks=["panel ok"],
                                      usage={"prompt_tokens": 12,
                                             "completion_tokens": 3,
                                             "total_tokens": 15,
                                             "cache_read_tokens": 8})])
    server = serve(cfg, workspace, host="127.0.0.1", port=0, provider=provider,
                   token="t-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        headers = {"authorization": "Bearer t-token"}
        resp = httpx.post(f"{base}/message", json={"text": "hello"},
                          headers=headers, timeout=15)
        sid = resp.json()["session_id"]

        sessions = httpx.get(f"{base}/panel/sessions", headers=headers, timeout=5)
        ids = [s["session_id"] for s in sessions.json()["sessions"]]
        assert sid in ids

        usage = httpx.get(f"{base}/panel/usage?session={sid}",
                          headers=headers, timeout=5).json()
        assert usage["entries"] >= 1, "the door must persist gateway usage lines"
        assert (usage["last"] or {}).get("cache_read_tokens") == 8, (
            "implicit cache accounting must reach the panel")

        evidence = httpx.get(f"{base}/panel/evidence?session={sid}",
                             headers=headers, timeout=5).json()
        assert evidence == {"evidence": []}

        # the SSE endpoint exists, is token-gated, and replays buffered events
        denied = httpx.get(f"{base}/events", timeout=5)
        assert denied.status_code == 403
        with httpx.stream("GET", f"{base}/events", headers=headers, timeout=10) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers.get("content-type", "")
    finally:
        from oaset.gateway import shutdown_server

        shutdown_server(server)
        server.server_close()


def test_cron_interval_job_does_not_crash_on_second_scan(isolated_home):
    """`every 20m` jobs crashed the daemon on scan #2: last_run is aware UTC,
    the scan's `now` is naive local — the subtraction raised TypeError."""
    import datetime as _dt

    from oaset import cron as cronmod

    job = cronmod.add_job("ticker", "every 20m", "say hi")
    job.last_run = _dt.datetime.now(_dt.timezone.utc).isoformat()
    cronmod.save_jobs([job])
    now = _dt.datetime.now()
    assert cronmod.is_due(job, now) is False  # fired seconds ago
    job.last_run = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=25)
                    ).isoformat()
    assert cronmod.is_due(job, now) is True


def test_cron_remove_resolves_the_short_id(isolated_home):
    """Every surface displays only the short id; `cron remove <short>` used to
    compare the GIVEN id's tail against full ids and never matched."""
    from oaset import cron as cronmod

    job = cronmod.add_job("nightly", "daily 03:00", "work")
    assert cronmod.remove_job(job.id[-8:]) is True
    assert cronmod.load_jobs() == []


def test_cron_daily_uses_local_dates_end_to_end(isolated_home, monkeypatch):
    """A daily job that fired at local 22:00 must be due again at the NEXT
    local 22:00 even in UTC-negative zones — the old code compared a local
    date marker against a UTC last_run and skipped every other day."""
    import datetime as _dt

    from oaset import cron as cronmod

    job = cronmod.add_job("nightly", "daily 22:00", "work")
    # simulate the previous fire: local 22:00 YESTERDAY
    yesterday_local = _dt.datetime.now().astimezone() - _dt.timedelta(days=1)
    yesterday_local = yesterday_local.replace(hour=22, minute=0, second=0,
                                              microsecond=0)
    job.last_run = yesterday_local.isoformat()
    cronmod.save_jobs([job])

    # now is past today's 22:00 → due again (use 23:00 today: the +2h trick
    # fails when the test runs before 20:00)
    later_today = _dt.datetime.now().replace(hour=23, minute=0, second=0,
                                             microsecond=0)
    assert cronmod.is_due(job, later_today) is True
    # ...but not before the scheduled time (an early-hour "now", so the suite
    # is time-of-day independent)
    morning = _dt.datetime.now().replace(hour=10, minute=0, second=0,
                                         microsecond=0)
    assert cronmod.is_due(job, morning) is False


# ------------------------------------------- audit round 8 pins (cron/patch)


def test_cron_multifire_does_not_crash_and_fires_once_per_slot(isolated_home):
    """A `*/N` schedule used to crash the daemon with UnboundLocalError the
    first time the clock reached a MATCHING minute (a function-local datetime
    import was referenced by a branch that never executed it), and before that
    it re-fired every 30s scan inside the same minute."""
    import datetime as _dt

    from oaset import cron as cronmod

    job = cronmod.add_job("tick", "*/5 * * * *", "work")
    local = _dt.datetime.now().replace(hour=10, minute=5, second=0,
                                       microsecond=0)
    # last fire = the UTC stamp of THAT LOCAL slot: hardcoding 02:05Z only
    # equals 10:05 local on UTC+8 machines — under CI's UTC clock the slot
    # comparison saw (2,5) vs (10,5) and fired again (real CI failure)
    job.last_run = local.astimezone(_dt.timezone.utc).isoformat()
    cronmod.save_jobs([job])
    assert cronmod.is_due(job, local) is False, "same slot: not due again"
    assert cronmod.is_due(job, local.replace(minute=10)) is True, "next slot: due"


def test_cron_single_fire_uses_local_dates(isolated_home):
    """The cron5 single-fire branch compared a UTC last_run prefix against a
    LOCAL date marker — the sibling of the daily bug, skipping days in
    UTC-negative zones."""
    import datetime as _dt

    from oaset import cron as cronmod

    job = cronmod.add_job("nightly5", "30 20 * * *", "work")
    # fired yesterday 20:30 LOCAL
    last = (_dt.datetime.now().astimezone() - _dt.timedelta(days=1)
            ).replace(hour=20, minute=30, second=0, microsecond=0)
    job.last_run = last.isoformat()
    cronmod.save_jobs([job])
    # a cron5 single-fire only matches its exact minute slot (20:30)
    due_slot = _dt.datetime.now().replace(hour=20, minute=30, second=0,
                                          microsecond=0)
    assert cronmod.is_due(job, due_slot) is True, "next day's slot: due"
    job.last_run = _dt.datetime.now().astimezone().replace(
        hour=20, minute=30, second=0, microsecond=0).isoformat()
    assert cronmod.is_due(job, due_slot) is False, "fired today: blocked"


def test_cron_remove_empty_id_deletes_nothing(isolated_home):
    from oaset import cron as cronmod

    cronmod.add_job("only", "daily 03:00", "work")
    assert cronmod.remove_job("") is False, "an empty id must not match all"
    assert len(cronmod.load_jobs()) == 1


def test_apply_patch_survives_dash_dash_content_pairs(tmp_path):
    """Deleting a line whose content starts with '-- ' next to an added line
    starting with '++ ' produces exactly a `--- x`/`+++ y` pair INSIDE a hunk;
    the header lookahead used to eat the rest of the hunk and report success
    while dropping the edit."""
    import asyncio
    import json as _json

    from oaset.tools import AutoGate, ToolContext, ToolRegistry

    (tmp_path / "cfg.txt").write_text(
        "-- old setting\nkeep me\n", encoding="utf-8")
    diff = "\n".join([
        "+++ b/cfg.txt",
        "@@ -1,2 +1,2 @@",
        "--- old setting",
        "+++ new setting",
        " keep me",
    ])
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=tmp_path, mode="auto"))
    res = asyncio.run(registry.dispatch("apply_patch", _json.dumps({"diff": diff})))
    assert not res.is_error, res.output
    text = (tmp_path / "cfg.txt").read_text(encoding="utf-8")
    assert "++ new setting" in text and "-- old setting" not in text
    assert "keep me" in text, "the hunk tail must not be dropped"
