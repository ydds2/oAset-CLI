"""Regression tests for the gap-fix batch (session/queue/search fixes)."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from oaset.config import AppConfig, load_config, save_config, shell_config
from oaset.kernel import AgentKernel
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.session import SessionStore
from oaset.tools import AutoGate, ToolContext, ToolRegistry
from oaset.tools.danger import classify_command
from tests.test_app_tui import chat_text, make_app, wait_until  # noqa: F401

# ---------------------------------------------------------------- B1 mcp-serve

def test_serve_stdio_accepts_token():
    import inspect

    from oaset.mcp_server import serve_stdio

    params = inspect.signature(serve_stdio).parameters
    assert "token" in params


def test_mcp_server_token_rejects_stranger():
    from oaset.mcp_server import McpServerCore

    core = McpServerCore(ToolRegistry(gate=AutoGate()), token="sekrit")
    core.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"clientInfo": {"token": "wrong"}}})
    result = core.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert "error" in result and "authenticated" in result["error"]["message"]


# ---------------------------------------------------------------- B3 config ui

def test_config_without_ui_table_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("OASET_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('default_model = "glm/glm-5.3-flash"\n', encoding="utf-8")
    cfg = load_config()
    assert cfg.default_model == "glm/glm-5.3-flash"
    assert cfg.ui.theme


# ---------------------------------------------------------------- B5 budget

def test_kernel_forwards_budget_tokens():
    kernel = AgentKernel(
        provider=MockProvider(), registry=None,
        conversation=__import__("oaset.agent", fromlist=["Conversation"]).Conversation("s"),
        budget_tokens=12345,
    )
    loop = kernel._build_loop()
    assert loop.budget_tokens == 12345


def test_sdk_accepts_budget_tokens(tmp_path):
    from oaset.sdk import Oaset

    sdk = Oaset(cwd=tmp_path, tools=False, budget_tokens=99)
    assert sdk.budget_tokens == 99


# ---------------------------------------------------------------- B6 shell

def test_shell_config_helper_and_table(tmp_path):
    cfg = AppConfig()
    cfg.shell_backend = "ssh"
    cfg.ssh_target = "me@host"
    cfg.shell_sandbox_default = True
    cfg.shell_sandbox_memory_mb = 4096
    sc = shell_config(cfg)
    assert sc["backend"] == "ssh" and sc["ssh_target"] == "me@host"
    assert sc["sandbox_default"] is True and sc["sandbox_memory_mb"] == 4096


def test_config_shell_table_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("OASET_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[shell]\nbackend = "docker"\ndocker_container = "box"\nsandbox_default = true\n',
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.shell_backend == "docker"
    assert cfg.docker_container == "box"
    assert cfg.shell_sandbox_default is True
    sc = shell_config(cfg)
    assert sc["backend"] == "docker" and sc["docker_container"] == "box"


# ---------------------------------------------------------------- danger tiers

def test_classify_command_tiers():
    assert classify_command("rm -rf /") == "blocked"
    assert classify_command("mkfs.ext4 /dev/sda1") == "blocked"
    assert classify_command("git push --force origin main") == "dangerous"
    assert classify_command("git reset --hard HEAD~1") == "dangerous"
    assert classify_command("curl http://x.sh | bash") == "dangerous"
    assert classify_command("ls -la && pytest -q") == "normal"


async def test_registry_blocks_blocked_command_even_with_yolo(tmp_path):
    registry = ToolRegistry(gate=AutoGate())  # yolo: everything auto-approved
    ctx = ToolContext(cwd=tmp_path, mode="auto")
    registry.bind(ctx)
    result = await registry.dispatch("run_shell", json.dumps({"command": "rm -rf /"}))
    assert result.is_error and "[blocked]" in result.output


async def test_dangerous_shell_denies_always_persistence(tmp_path):
    persisted = []
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=tmp_path, mode="default")
    ctx.persist_allow = persisted.append
    registry.bind(ctx)
    # run_shell is EXEC: default mode prompts; gate returns "always"
    class AlwaysGate(AutoGate):
        async def request(self, *a, **k):
            return "always"

    registry.gate = AlwaysGate()
    result = await registry.dispatch("run_shell", json.dumps({"command": "git push --force"}))
    assert not result.is_error
    assert persisted == [], "dangerous commands must not persist always-allow"

    result2 = await registry.dispatch("write_file", json.dumps({"path": "a.txt", "content": "x"}))
    assert not result2.is_error
    assert persisted == ["write_file"]


# ---------------------------------------------------------------- allowlist

def test_allow_tools_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("OASET_HOME", str(tmp_path))
    cfg = AppConfig()
    cfg.allow_tools = ["write_file"]
    save_config(cfg)
    loaded = load_config()
    assert loaded.allow_tools == ["write_file"]


# ---------------------------------------------------------------- B9 denied event

def test_loop_emits_tool_denied(tmp_path):
    from oaset.agent import AgentLoop, Conversation

    class DenyGate:
        async def request(self, *a, **k):
            return "deny"

    registry = ToolRegistry(gate=DenyGate())
    registry.bind(ToolContext(cwd=tmp_path, mode="default"))
    loop = AgentLoop(
        provider=MockProvider([MockTurn(tool_calls=[MockToolCall("write_file", {"path": "x", "content": "y"})]),
                               MockTurn(content_chunks=["ok"])]),
        registry=registry, conversation=Conversation("s"),
    )
    events = []
    loop.run = loop.run  # keep reference
    import asyncio

    async def main():
        await loop.run("go", events.append)

    asyncio.run(main())
    kinds = [e["type"] for e in events]
    assert "tool_denied" in kinds


# ---------------------------------------------------------------- /title backend

def test_store_set_title(tmp_path):
    from tests.test_app_tui import make_app  # noqa: F401 (used below)

    store = SessionStore(home=tmp_path)
    session = store.new_session(tmp_path, "glm/glm-5.3-flash")
    from oaset.agent.messages import Message

    store.append_message(session, Message(role="user", content="hello"))
    assert store.set_title(session.meta.session_id, "my custom title")
    reloaded = store.load(session.meta.session_id)
    assert reloaded.meta.title == "my custom title"
    metas = store.list_sessions(cwd=str(tmp_path))
    assert any(m.title == "my custom title" for m in metas)


# ---------------------------------------------------------------- B10 i18n

def test_i18n_no_duplicate_keys():
    import re

    from oaset.i18n import CATALOG

    # CATALOG is built from the source dict; duplicates would collapse silently,
    # so re-parse the source to prove there are none.
    src = Path("src/oaset/i18n.py").read_text(encoding="utf-8")
    keys = re.findall(r'^    "([a-z_]+)": \{', src, re.MULTILINE)
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes
    assert CATALOG


# ---------------------------------------------------------------- background tasks

async def test_background_task_lifecycle(tmp_path):

    from oaset.tools.background import get_registry
    from oaset.tools.tasks import TaskListTool, TaskOutputTool, TaskStopTool

    ctx = ToolContext(cwd=tmp_path, mode="auto")
    registry = ToolRegistry(gate=AutoGate(), tools=[])
    registry.bind(ctx)

    from oaset.tools.shell import RunShellTool

    # run_shell needs exec tools; start via subprocess directly through the tool
    shell = RunShellTool()
    res = await shell.run({"command": "python -c \"print(42)\"", "run_in_background": True}, ctx)
    assert not res.is_error
    assert "task-1" in res.output

    tasks = TaskListTool()
    listing = await tasks.run({"active_only": True}, ctx)
    assert "task-1" in listing.output

    out = TaskOutputTool()
    text = await out.run({"task_id": "task-1", "block": True, "timeout": 20}, ctx)
    assert "completed" in text.output.splitlines()[0]
    assert "42" in text.output

    # stop path on a long-running task
    res2 = await shell.run({"command": "python -c \"import time; time.sleep(60)\"", "run_in_background": True}, ctx)
    assert "task-2" in res2.output
    stop = TaskStopTool()
    stopped = await stop.run({"task_id": "task-2"}, ctx)
    assert "stopped" in stopped.output
    reg = get_registry(ctx)
    assert reg.get("task-2").status == "stopped"


async def test_run_shell_background_via_registry(tmp_path):
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=tmp_path, mode="auto")
    registry.bind(ctx)
    res = await registry.dispatch("run_shell", json.dumps({"command": "echo hi-bg", "run_in_background": True}))
    assert not res.is_error and "task-1" in res.output
    out = await registry.dispatch("task_output", json.dumps({"task_id": "task-1", "block": True, "timeout": 10}))
    assert "hi-bg" in out.output


# ---------------------------------------------------------------- live streaming

class _GateProvider:
    """Emits one delta, then blocks until the consumer proves it saw the event."""

    def __init__(self):
        import asyncio

        self.seen = asyncio.Event()

    async def stream(self, messages, tools=None):
        import asyncio

        from oaset.providers.base import ContentDelta, StreamDone

        yield ContentDelta(text="first ")
        await asyncio.wait_for(self.seen.wait(), timeout=5.0)
        yield ContentDelta(text="second")
        yield StreamDone(finish_reason="stop", usage={"total_tokens": 7})


async def test_kernel_chat_is_live_not_buffered():

    from oaset.agent import Conversation
    from oaset.kernel import AgentKernel

    provider = _GateProvider()
    kernel = AgentKernel(provider=provider, registry=None, conversation=Conversation("s"))
    got = []
    async for event in kernel.chat("hi"):
        got.append(event)
        if event.type == "assistant_delta" and not provider.seen.is_set():
            provider.seen.set()  # only possible if delivery is live
    assert got[-1].type == "turn_completed"
    deltas = [e for e in got if e.type == "assistant_delta"]
    assert [d.data["text"] for d in deltas] == ["first ", "second"]
    assert any("second" in str(e.data) for e in got)


async def test_sdk_chat_is_live_not_buffered(tmp_path):
    from oaset.sdk import Oaset

    provider = _GateProvider()
    sdk = Oaset(cwd=tmp_path, provider=provider, tools=False)
    got = []
    async for kind, data in sdk.chat("hi"):
        got.append((kind, data))
        if kind == "assistant_delta" and not provider.seen.is_set():
            provider.seen.set()
    assert "assistant_delta" in [k for k, _ in got]
    assert got[-1][0] == "turn_completed"


async def test_stream_events_cancels_producer_on_abandon():
    import asyncio

    from oaset.kernel import stream_events

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def produce(emit):
        started.set()
        try:
            for i in range(200):
                emit({"i": i})
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    got = 0
    async for _ in stream_events(produce):
        got += 1
        if got == 3:
            break  # abandon early
    assert got == 3
    assert await asyncio.wait_for(cancelled.wait(), timeout=2.0)


# ---------------------------------------------------------------- C-3 MCP http + filtering

def _json_response(payload: dict):
    import httpx as _hx

    return _hx.Response(200, headers={"mcp-session-id": "sess-1"}, content=json.dumps(payload))


def test_mcp_http_connection_roundtrip():
    import asyncio

    import httpx

    from oaset.mcp.http import McpHttpConnection

    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_headers.update(request.headers)
        method = body.get("method")
        if method == "initialize":
            return _json_response({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "fake"}}})
        if method == "tools/list":
            return _json_response({"jsonrpc": "2.0", "id": 100, "result": {"tools": [
                {"name": "echo", "description": "echo it",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]}})
        if method == "tools/call":
            return _json_response({"jsonrpc": "2.0", "id": 100, "result": {
                "content": [{"type": "text", "text": "echo: hi"}], "isError": False}})
        if method == "ping":
            return _json_response({"jsonrpc": "2.0", "id": 100, "result": {}})
        return _json_response({"jsonrpc": "2.0", "id": 100, "error": {"message": "nope"}})

    transport = httpx.MockTransport(handler)
    conn = McpHttpConnection(name="fake", url="http://mcp.test/mcp", client=httpx.AsyncClient(transport=transport))

    async def main():
        await conn.start()
        assert conn._session_id == "sess-1"
        assert seen_headers.get("mcp-session-id") == "sess-1"
        tools = await conn.list_tools()
        assert [t.name for t in tools] == ["echo"]
        result = await conn.call_tool("echo", {"text": "hi"})
        assert result.text == "echo: hi" and not result.is_error
        assert await conn.ping() is True
        await conn.close()


    asyncio.run(main())


def test_mcp_http_sse_body_accepted():
    import httpx

    from oaset.mcp.http import McpHttpConnection

    sse = 'event: message\r\ndata: {"jsonrpc":"2.0","id":100,"result":{"tools":[]}}\r\n\r\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"mcp-session-id": "s2"})

    conn = McpHttpConnection(name="fake", url="http://mcp.test/mcp",
                             client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def main():
        await conn.start()
        tools = await conn.list_tools()
        assert tools == []
        await conn.close()

    import asyncio

    asyncio.run(main())


def test_filter_remote_tools():
    from oaset.mcp.client import McpToolDef
    from oaset.mcp.http import filter_remote_tools

    tools = [McpToolDef("a", "", {}), McpToolDef("b", "", {}), McpToolDef("c", "", {})]
    assert [t.name for t in filter_remote_tools(tools)] == ["a", "b", "c"]
    assert [t.name for t in filter_remote_tools(tools, enabled=["a", "c"])] == ["a", "c"]
    assert [t.name for t in filter_remote_tools(tools, disabled=["b"])] == ["a", "c"]
    assert [t.name for t in filter_remote_tools(tools, enabled=["a", "b"], disabled=["b"])] == ["a"]


def test_load_server_specs_http_entries(tmp_path, monkeypatch):
    from oaset.mcp.manager import load_server_specs

    monkeypatch.setenv("MCP_TOK", "envtoken")
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {
        "local1": {"command": "python", "args": ["-m", "srv"], "disabledTools": ["x"]},
        "remote1": {"url": "http://h/mcp", "bearerTokenEnvVar": "MCP_TOK",
                    "enabledTools": ["search"], "startupTimeoutMs": 9000},
        "off": {"url": "http://h/mcp", "enabled": False},
    }}), encoding="utf-8")
    specs = {s.name: s for s in load_server_specs(tmp_path)}
    assert set(specs) == {"local1", "remote1"}
    assert specs["local1"].kind == "stdio" and specs["local1"].disabled_tools == ["x"]
    r = specs["remote1"]
    assert r.kind == "http" and r.headers.get("Authorization") == "Bearer envtoken"
    assert r.enabled_tools == ["search"] and r.startup_timeout_ms == 9000


# ---------------------------------------------------------------- C-4 prompt caching

def _stop_handler(request: httpx.Request) -> httpx.Response:
    sse = 'data: {"type":"message_stop"}' + chr(10) * 2
    return httpx.Response(200, text=sse)


def test_anthropic_cache_breakpoints():
    from oaset.providers.anthropic_native import AnthropicNativeProvider, _to_wire

    messages = [
        {"role": "system", "content": "you are oaset"},
        {"role": "user", "content": "read x"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "contents"},
        {"role": "user", "content": "now summarize"},
    ]
    system, converted = _to_wire(messages)
    provider = AnthropicNativeProvider(
        model_id="anthropic/claude-sonnet", api_key="k", prompt_cache=True,
        client=httpx.AsyncClient(transport=httpx.MockTransport(_stop_handler)),
    )
    captured = {}

    real_build = provider._client.build_request

    def build(method, url, json=None, **kw):
        captured.update(json=json)
        return real_build(method, url, json=json, **kw)

    provider._client.build_request = build

    async def drain():
        async for _ in provider.stream(messages, tools=None):
            break

    asyncio.run(drain())
    sys_block = captured["json"]["system"]
    assert isinstance(sys_block, list) and sys_block[0]["cache_control"] == {"type": "ephemeral"}
    wire_msgs = captured["json"]["messages"]
    # rolling breakpoint rides the NEWEST user turn, whatever form it takes —
    # here a plain typed message after a tool result, which must upgrade to
    # blocks and carry the marker (stale placement on the older tool_result
    # would leave the typed turn's prefix uncached)
    last_user = wire_msgs[-1]
    assert last_user["role"] == "user"
    assert isinstance(last_user["content"], list)
    assert last_user["content"][-1]["text"] == "now summarize"
    assert last_user["content"][-1]["cache_control"] == {"type": "ephemeral"}
    tool_msgs = [m for m in wire_msgs if m["role"] == "user" and isinstance(m.get("content"), list)
                 and m["content"] and m["content"][0].get("type") == "tool_result"]
    # the SECOND rolling marker writes the previous boundary too — an idle
    # gap that outlives the newest entry's 5-minute TTL still hits one
    # turn back instead of reprocessing the whole conversation
    assert tool_msgs and tool_msgs[-1]["content"][0].get("cache_control") == {
        "type": "ephemeral"}


def test_anthropic_cache_off_by_default():
    from oaset.providers.anthropic_native import AnthropicNativeProvider

    provider = AnthropicNativeProvider(
        model_id="anthropic/claude-sonnet", api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(_stop_handler)),
    )
    captured = {}
    real_build = provider._client.build_request

    def build(method, url, json=None, **kw):
        captured.update(json=json)
        return real_build(method, url, json=json, **kw)

    provider._client.build_request = build

    async def drain():
        async for _ in provider.stream(
            [{"role": "system", "content": "s"},
             {"role": "user", "content": "u"}], tools=None):
            break

    asyncio.run(drain())
    assert captured["json"]["system"] == "s"
    assert "prompt_cache" not in captured["json"]


# ---------------------------------------------------------------- C-5 fallback chain

class _AlwaysFailingProvider:
    model_id = "failing/primary"

    async def stream(self, messages, tools=None):
        from oaset.providers.base import ProviderError

        raise ProviderError("server", "primary on fire", retryable=True)
        yield  # pragma: no cover - makes this an async generator


async def test_loop_switches_to_fallback_after_retries(tmp_path):
    from oaset.agent import AgentLoop, Conversation

    good = MockProvider([MockTurn(content_chunks=["rescued by fallback"])])
    loop = AgentLoop(
        provider=_AlwaysFailingProvider(),
        registry=None,
        conversation=Conversation("s"),
        fallbacks=[_AlwaysFailingProvider(), good],
    )
    events = []

    async def main():
        await loop.run("hi", events.append)

    await asyncio.wait_for(main(), timeout=15)
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "turn_done"
    notices = [e["text"] for e in events if e["type"] == "notice"]
    assert any("fallback" in t or "备用" in t for t in notices)
    assert loop.provider is good
    assert any("rescued by fallback" in e.get("content", "") for e in events if e["type"] == "turn_done")


async def test_loop_raises_when_fallbacks_exhausted(tmp_path):
    from oaset.agent import AgentLoop, Conversation

    loop = AgentLoop(
        provider=_AlwaysFailingProvider(),
        registry=None,
        conversation=Conversation("s"),
        fallbacks=[],
    )
    events = []

    async def main():
        await loop.run("hi", events.append)

    with pytest.raises(Exception):
        await asyncio.wait_for(main(), timeout=15)


def test_build_provider_chain_skips_primary_and_invalid(tmp_path, monkeypatch):
    from oaset.config import build_provider_chain, load_config, save_config

    monkeypatch.setenv("OASET_HOME", str(tmp_path))
    from oaset.config import default_config

    cfg = default_config()
    cfg.fallback_models = ["mock/mock-echo", "nope/missing", "mock/mock-echo"]
    save_config(cfg)
    cfg = load_config()
    model = cfg.models["mock/mock-echo"]
    primary, fallbacks = build_provider_chain(cfg, model)
    assert primary is not None
    assert fallbacks == []  # primary skipped, invalid skipped


def test_fallback_models_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("OASET_HOME", str(tmp_path))
    cfg = AppConfig()
    cfg.fallback_models = ["glm/glm-5.3-flash"]
    save_config(cfg)
    assert load_config().fallback_models == ["glm/glm-5.3-flash"]


# ---------------------------------------------------------------- C-6 /rewind

def test_store_rewind_turns(tmp_path):
    from oaset.agent.messages import Message

    store = SessionStore(home=tmp_path)
    session = store.new_session(tmp_path, "glm/glm-5.3-flash")
    for user, reply in [("q1", "a1"), ("q2", "a2"), ("q3", "a3")]:
        # mirror production: append to the conversation first, then persist
        session.conversation.append(Message(role="user", content=user))
        store.append_message(session, Message(role="user", content=user))
        session.conversation.append(Message(role="assistant", content=reply))
        store.append_message(session, Message(role="assistant", content=reply))
    assert len(session.conversation.messages) == 6

    remaining = store.rewind_turns(session, 1)
    assert remaining == 4
    assert [m.content for m in session.conversation.messages] == ["q1", "a1", "q2", "a2"]

    # persisted file matches: reload from disk
    reloaded = store.load(session.meta.session_id)
    assert [m.content for m in reloaded.conversation.messages] == ["q1", "a1", "q2", "a2"]

    with pytest.raises(ValueError):
        store.rewind_turns(session, 99)

    remaining = store.rewind_turns(session, 2)
    assert remaining == 0


async def test_rewind_command_rehydrates_view(workspace):
    from oaset.providers import MockTurn
    from oaset.tui.widgets.palette import InlineCommandConfirm
    from tests.test_app_tui import make_app
    app = make_app(workspace, [MockTurn(content_chunks=["answer one"]), MockTurn(content_chunks=["answer two"])])
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("question one")
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: "answer one" in chat_text(app))
        app.input_area.load_text("question two")
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: "answer two" in chat_text(app))

        # `/rewind` with no argument asks how many turns first (ARG_PROMPTS),
        # then confirms; `/rewind 1` skips straight to the confirmation. Drive
        # the explicit form here so the assertion is about the truncation.
        app.submit_text("/rewind 1")
        # /rewind is RISK_DANGER and its argless path is a numeric prompt, not a
        # picker, so a confirmation page stands between the command and the
        # truncation: nothing is dropped until the user answers it.
        assert await wait_until(pilot, lambda: bool(app.query(InlineCommandConfirm))), \
            "the danger confirmation page must open for /rewind"
        assert "answer two" in chat_text(app), "nothing may be dropped before the answer"
        await pilot.press("y")
        assert await wait_until(pilot, lambda: "answer two" not in chat_text(app))
        assert "answer one" in chat_text(app)
        assert len(app.conversation.messages) == 2  # q1 + a1

        # And declining the confirmation must leave the transcript untouched.
        app.submit_text("/rewind 1")
        assert await wait_until(pilot, lambda: bool(app.query(InlineCommandConfirm)))
        await pilot.press("n")
        await pilot.pause(0.5)
        assert len(app.conversation.messages) == 2, "declining must not truncate"


# ---------------------------------------------------------------- C-7 permission rules

def _rule_ctx(tmp_path, rules):
    ctx = ToolContext(cwd=tmp_path, mode="default")
    ctx.rules = rules
    return ctx


async def test_rule_allow_bypasses_gate(tmp_path):
    from oaset.tools.base import PermissionRule

    class DenyGate(AutoGate):
        async def request(self, *a, **k):
            raise AssertionError("allow rule must bypass the gate")

    registry = ToolRegistry(gate=DenyGate())
    ctx = _rule_ctx(tmp_path, [PermissionRule("allow", "write_file", reason="trusted")])
    registry.bind(ctx)
    (tmp_path / "ok.txt").write_text("", encoding="utf-8")
    res = await registry.dispatch("write_file", json.dumps({"path": "ok.txt", "content": "data"}))
    assert not res.is_error


async def test_rule_deny_blocks_even_yolo(tmp_path):
    from oaset.tools.base import PermissionRule

    registry = ToolRegistry(gate=AutoGate())  # yolo
    ctx = _rule_ctx(tmp_path, [PermissionRule("deny", "run_shell")])
    registry.bind(ctx)
    res = await registry.dispatch("run_shell", json.dumps({"command": "echo hi"}))
    assert res.is_error and "[denied by rule]" in res.output


async def test_rule_subject_glob(tmp_path):
    from oaset.tools.base import PermissionRule

    class RecordingGate(AutoGate):
        calls = []
        async def request(self, tool_name, level, summary, preview=None):
            RecordingGate.calls.append(summary)
            return "deny"

    registry = ToolRegistry(gate=RecordingGate())
    ctx = _rule_ctx(tmp_path, [PermissionRule("ask", "run_shell(npm run *)")])
    registry.bind(ctx)
    res = await registry.dispatch("run_shell", json.dumps({"command": "npm run dev"}))
    assert res.is_error and RecordingGate.calls  # forced ask -> gate even in default+allowed? default mode asks anyway
    res2 = await registry.dispatch("run_shell", json.dumps({"command": "npm test"}))
    assert res2.is_error  # no rule match -> normal gate -> deny


async def test_rule_ask_forces_prompt_despite_session_allow(tmp_path):
    from oaset.tools.base import PermissionRule

    prompted = []

    class RecordingGate(AutoGate):
        async def request(self, tool_name, level, summary, preview=None):
            prompted.append(tool_name)
            return "allow"

    registry = ToolRegistry(gate=RecordingGate())
    ctx = _rule_ctx(tmp_path, [PermissionRule("ask", "write_file")])
    ctx.session_allowed.add("write_file")  # would normally skip the gate
    registry.bind(ctx)
    res = await registry.dispatch("write_file", json.dumps({"path": "f.txt", "content": "x"}))
    assert not res.is_error and prompted == ["write_file"]


def test_config_permission_rules_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("OASET_HOME", str(tmp_path))
    from oaset.config import default_config, load_config, save_config

    cfg = default_config()
    cfg.permission_rules = [
        __import__("oaset.tools.base", fromlist=["PermissionRule"]).PermissionRule(
            "deny", "run_shell(rm *)", scope="user", reason="no deletions"),
        __import__("oaset.tools.base", fromlist=["PermissionRule"]).PermissionRule(
            "allow", "write_file"),
    ]
    save_config(cfg)
    loaded = load_config()
    assert len(loaded.permission_rules) == 2
    assert loaded.permission_rules[0].decision == "deny"
    assert loaded.permission_rules[0].pattern == "run_shell(rm *)"
    assert loaded.permission_rules[1].decision == "allow"


async def test_rule_ask_forces_read_fence_confirm(tmp_path):
    from oaset.tools.base import PermissionRule

    outside = tmp_path.parent / f"oaset-ask-read-{tmp_path.name[:6]}"
    outside.mkdir(exist_ok=True)
    (outside / "r.txt").write_text("x", encoding="utf-8")
    try:
        prompted = []

        class RecordingGate(AutoGate):
            async def request(self, tool_name, level, summary, preview=None):
                prompted.append(tool_name)
                return "allow"

        registry = ToolRegistry(gate=RecordingGate())
        ctx = _rule_ctx(tmp_path, [PermissionRule("ask", "read_file")])
        registry.bind(ctx)
        res = await registry.dispatch("read_file", json.dumps({"path": str(outside / "r.txt")}))
        assert not res.is_error
        assert prompted == ["read_file"]
    finally:
        import shutil as _sh

        _sh.rmtree(outside, ignore_errors=True)


# ---------------------------------------------------------------- D1 ask/plan/bg-notify

async def test_ask_user_tool_roundtrip(tmp_path):
    from oaset.tools.ask_user import AskUserQuestionTool

    ctx = ToolContext(cwd=tmp_path, mode="auto")

    async def gateway(question, choices):
        assert question == "which db?"
        assert choices == ["sqlite", "postgres"]
        return "postgres"

    ctx.session_state["ask_user"] = gateway
    tool = AskUserQuestionTool()
    res = await tool.run({"question": "which db?", "choices": ["sqlite", "postgres"]}, ctx)
    assert not res.is_error and "postgres" in res.output


async def test_ask_user_headless_graceful(tmp_path):
    from oaset.tools.ask_user import AskUserQuestionTool

    ctx = ToolContext(cwd=tmp_path, mode="auto")
    res = await AskUserQuestionTool().run({"question": "q", "choices": ["a"]}, ctx)
    assert res.is_error and "No interactive user" in res.output


async def test_plan_mode_tools(tmp_path):
    from oaset.tools.ask_user import EnterPlanModeTool, ExitPlanModeTool

    current = {"value": "default"}
    ctx = ToolContext(cwd=tmp_path, mode="default")

    def set_mode(mode):
        current["value"] = mode
        ctx.mode = mode

    ctx.session_state["set_mode"] = set_mode
    res = await EnterPlanModeTool().run({}, ctx)
    assert "Plan mode ON" in res.output and current["value"] == "plan" and ctx.mode == "plan"
    res = await ExitPlanModeTool().run({}, ctx)
    assert "OFF" in res.output and current["value"] == "default" and ctx.mode == "default"


def test_background_drain_completed_once():
    import asyncio

    from oaset.tools.background import BackgroundRegistry

    reg = BackgroundRegistry()

    async def main():
        proc = await asyncio.create_subprocess_exec(
            "python", "-c", "print(1)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        reg.start_process(proc, "demo")
        assert reg.drain_completed() == []  # still running
        await asyncio.wait_for(asyncio.shield(reg.tasks["task-1"].proc.wait()), timeout=10)
        done = reg.drain_completed()
        assert len(done) == 1 and done[0].id == "task-1"
        assert reg.drain_completed() == []  # reported exactly once

    asyncio.run(main())


async def test_loop_injects_background_completion(tmp_path):
    """Bg task finishes during turn 1; the notice is injected into turn 2."""
    import asyncio

    from oaset.agent import AgentLoop, Conversation
    from oaset.providers import MockProvider, MockToolCall, MockTurn
    from oaset.tools.background import BackgroundRegistry

    script = [
        MockTurn(tool_calls=[MockToolCall("run_shell", {
            "command": "python -c \"import time; time.sleep(0.2)\"", "run_in_background": True})]),
        MockTurn(content_chunks=["started in background"]),
        MockTurn(content_chunks=["turn two reply"]),
    ]
    class Slow(MockProvider):
        async def stream(self, messages, tools=None):
            async for ev in super().stream(messages, tools):
                await asyncio.sleep(0.15)
                yield ev

    reg = BackgroundRegistry()
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=tmp_path, mode="auto")
    ctx.session_state["background_tasks"] = reg
    registry.bind(ctx)
    loop = AgentLoop(provider=Slow(script), registry=registry,
                     conversation=Conversation("s"), max_iterations=10)
    events = []

    async def main():
        await loop.run("start bg task", events.append)
        await asyncio.sleep(0.6)          # task completes after the turn ends
        await loop.run("and now?", events.append)  # next turn drains the notice

    await asyncio.wait_for(main(), timeout=20)
    msgs = [e.get("message") for e in events if e["type"] == "message_done"]
    assert any("background task-1" in str(getattr(m, "content", "")) for m in msgs), msgs



# ---------------------------------------------------------------- D2 @mention + paste collapse

async def test_at_file_mention_completion(workspace):
    from oaset.utils import workspace_files

    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "src" / "main.py").write_text("x", encoding="utf-8")
    (workspace / "notes.md").write_text("x", encoding="utf-8")
    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("see @main")
        await pilot.pause(0.2)
        assert app.input_area._suggest_visible
        assert any("main.py" in name for name, _ in app.input_area._suggest_options)
        await pilot.press("enter")  # accept -> token replaced with the path
        await pilot.pause(0.2)  # accept runs after the IME-settle delay
        assert "src/main.py" in app.input_area.text
        assert workspace_files(workspace, "notes") == ["notes.md"]


async def test_large_paste_collapses_to_file(workspace):
    from textual import events

    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("")
        big = "x" * 3000
        app.input_area.post_message(events.Paste(big))
        await pilot.pause(0.3)
        text = app.input_area.text
        assert "[pasted:" in text
        from oaset.utils import oaset_home

        pastes = list((oaset_home() / "pastes").glob("paste-*.txt"))
        assert pastes and pastes[0].read_text(encoding="utf-8") == big


# ---------------------------------------------------------------- D3 repo map

def test_repo_map_tool(tmp_path):
    from oaset.tools.repo_map import RepoMapTool

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "core.py").write_text(
        'class Engine:\n    """doc"""\n\n\ndef run():\n    pass\n', encoding="utf-8")
    (tmp_path / "README.md").write_text("# My Project\n\nbody\n", encoding="utf-8")
    (tmp_path / "config.toml").write_text('[tool]\nname = "x"\n', encoding="utf-8")

    ctx = ToolContext(cwd=tmp_path, mode="auto")
    ctx.output_limit = 2000
    res = RepoMapTool().run({"max_files": 10}, ctx)
    import asyncio

    out = asyncio.run(res).output
    assert "config.toml" in out and "[config]" in out
    assert "class Engine" in out
    assert "def run()" in out
    assert "# My Project" in out or "My Project" in out
    # node_modules pruned
    (tmp_path / "node_modules").mkdir(exist_ok=True)
    (tmp_path / "node_modules" / "junk.py").write_text("x = 1\n", encoding="utf-8")
    out2 = asyncio.run(RepoMapTool().run({"max_files": 50}, ctx)).output
    assert "junk" not in out2


# ---------------------------------------------------------------- D4 hooks upgrade

async def test_hook_http_action_posts_json(tmp_path, monkeypatch):
    import httpx

    from oaset.hooks import HookRunner

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw)
    )
    # hooks are PUSH-class: posting conversation content requires full mode
    runner = HookRunner({"on_turn_end": "http://collector.test/oaset"}, tmp_path,
                        network_mode="full")
    await runner.fire("on_turn_end", user_text="hello")
    assert captured["url"] == "http://collector.test/oaset"
    assert captured["json"]["event"] == "on_turn_end"
    assert captured["json"]["user_text"] == "hello"


def test_hook_new_events_accepted(tmp_path):
    from oaset.hooks import EVENTS, HookRunner

    runner = HookRunner({"on_turn_start": "echo a", "on_error": "echo b"}, tmp_path)
    assert runner.active
    assert "on_turn_start" in EVENTS and "on_error" in EVENTS


# ---------------------------------------------------------------- D4 trigram CJK

def test_trigram_cjk_search(tmp_path):
    from oaset import history
    from oaset.agent.messages import Message
    from oaset.session import SessionStore

    store = SessionStore(home=tmp_path)
    session = store.new_session(tmp_path, "glm/glm-5.3-flash")
    session.conversation.append(Message(role="user", content="帮我重构上下文压缩模块"))
    store.append_message(session, Message(role="user", content="帮我重构上下文压缩模块"))

    assert history.index_all(home=tmp_path) >= 1
    hits = history.search_history("上下文压缩", home=tmp_path)
    assert hits and "上下文压缩" in hits[0].snippet
    # short CJK substring (2 chars) also matches via trigram
    hits2 = history.search_history("压缩模块", home=tmp_path)
    assert hits2 and "压缩" in hits2[0].snippet


# ---------------------------------------------------------------- agent-type background tasks

async def test_task_run_in_background_agent(tmp_path):
    from oaset.tools.background import get_registry
    from oaset.tools.subagent import TaskTool

    async def runner(agent, prompt):
        await asyncio.sleep(0.2)
        return "SUB-REPORT: finished research"

    ctx = ToolContext(cwd=tmp_path, mode="auto")
    ctx.session_state["subagent_runner"] = runner
    ctx.session_state["agents"] = {}

    res = await TaskTool().run(
        {"agent": "general", "description": "research stuff", "prompt": "do research",
         "run_in_background": True}, ctx)
    assert not res.is_error and "agent-1" in res.output

    reg = get_registry(ctx)
    task = reg.get("agent-1")
    assert task.kind == "agent"
    await asyncio.wait_for(asyncio.shield(reg._done(task)), timeout=5)
    assert task.status == "completed"
    out = await reg.output("agent-1")
    assert "finished research" in out
    done = reg.drain_completed()
    assert [t.id for t in done] == ["agent-1"]


async def test_task_background_stop_cancels(tmp_path):
    import asyncio

    from oaset.tools.background import get_registry
    from oaset.tools.subagent import TaskTool

    async def runner(agent, prompt):
        await asyncio.sleep(60)

    ctx = ToolContext(cwd=tmp_path, mode="auto")
    ctx.session_state["subagent_runner"] = runner
    ctx.session_state["agents"] = {}

    res = await TaskTool().run(
        {"agent": "general", "description": "long", "prompt": "long work",
         "run_in_background": True}, ctx)
    assert "agent-1" in res.output
    reg = get_registry(ctx)
    stopped = await reg.stop("agent-1")
    assert "stopped" in stopped
    assert reg.get("agent-1").status == "stopped"


async def test_image_command_queues_attachment(workspace):
    img = workspace / "pic.png"
    img.write_bytes(b"\x89PNG fake")

    app = make_app(workspace, [])
    # patch the model config to advertise image_in

    app.model_cfg.capabilities = list(app.model_cfg.capabilities) + ["image_in"]
    async with app.run_test(size=(110, 36)) as pilot:
        # bare /image opens the workspace image FILE PICKER (structured form)
        from oaset.tui.widgets.inline import InlinePicker

        app.submit_text("/image")
        assert await wait_until(pilot, lambda: len(app.query(InlinePicker)) == 1)
        offered = [value for value, _ in app.query_one(InlinePicker).options]
        assert any(v.endswith("pic.png") for v in offered)
        await pilot.press("enter")  # pick pic.png (only image present)
        assert await wait_until(pilot, lambda: "已附加图片" in chat_text(app))
        pending, app._pending_images = app._pending_images, []
        assert [p.name for p in pending] == ["pic.png"]

        # explicit typed path still works (no picker involved)
        app.submit_text(f"/image {img}")
        assert await wait_until(pilot, lambda: len(app._pending_images) == 1)


def test_image_command_registry_drift():
    from oaset.tui.app import OasetApp
    from oaset.tui.commands import COMMANDS

    assert any(c.name == "image" for c in COMMANDS)
    assert hasattr(OasetApp, "cmd_image")


def test_rewind_creates_restorable_backup(tmp_path):
    from oaset.agent.messages import Message
    from oaset.session import SessionStore

    store = SessionStore(home=tmp_path)
    session = store.new_session(tmp_path, "glm/glm-5.3-flash")
    session.conversation.append(Message(role="user", content="q1"))
    store.append_message(session, Message(role="user", content="q1"))
    store.rewind_turns(session, 1)
    backups = list(tmp_path.glob("sessions/**/session_*.jsonl.pre-rewind*"))
    assert backups and "q1" in backups[0].read_text(encoding="utf-8")


# ------------------------------------------------ multi-file patches vs rules

async def test_patch_deny_rule_cannot_be_dodged_by_file_order(tmp_path):
    """Rules used to judge a multi-file patch by its FIRST file only, so
    listing the denied path second bypassed the deny entirely."""
    import json as _json

    from oaset.tools import AutoGate, ToolRegistry
    from oaset.tools.base import PermissionRule

    registry = ToolRegistry(gate=AutoGate())
    ctx = _rule_ctx(tmp_path, [PermissionRule("deny", "apply_patch(.ssh/*)")])
    registry.bind(ctx)
    diff = "\n".join([
        "--- a/src/app.py", "+++ b/src/app.py", "@@ -1 +1 @@",
        "-x = 1", "+x = 2",
        "--- a/.ssh/config", "+++ b/.ssh/config", "@@ -1 +1 @@",
        "-old", "+new",
    ])
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    res = await registry.dispatch("apply_patch", _json.dumps({"diff": diff}))
    assert res.is_error and "[denied by rule]" in res.output


async def test_patch_allow_rule_does_not_approve_unmatched_files(tmp_path):
    """An allow glob must cover EVERY file in the patch; anything it does not
    match falls through to the gate instead of riding along."""
    import json as _json

    from oaset.tools import ToolRegistry
    from oaset.tools.base import AutoGate, PermissionRule

    prompted: list[str] = []

    class RecordingGate(AutoGate):
        async def request(self, tool_name, level, summary, preview=None):
            prompted.append(f"{tool_name}: {summary}")
            return "allow"

    registry = ToolRegistry(gate=RecordingGate())
    ctx = _rule_ctx(tmp_path, [PermissionRule("allow", "apply_patch(src/*)")])
    registry.bind(ctx)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    diff = "\n".join([
        "+++ b/src/app.py", "@@ -1 +1 @@", "-x = 1", "+x = 2",
        "+++ b/secrets/key.py", "@@ -1 +1 @@", "-k = 1", "+k = 2",
    ])
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "key.py").write_text("k = 1\n", encoding="utf-8")
    res = await registry.dispatch("apply_patch", _json.dumps({"diff": diff}))
    assert not res.is_error
    assert prompted, "the unmatched file must reach the gate, not ride the allow rule"


async def test_read_tool_ask_rule_reaches_the_gate(tmp_path):
    """`ask` on a READ tool used to be swallowed: the READ branch only ever
    prompted for out-of-workspace reads, so the rule did nothing."""
    import json as _json

    from oaset.tools import ToolRegistry
    from oaset.tools.base import AutoGate, PermissionRule

    prompted: list[str] = []

    class RecordingGate(AutoGate):
        async def request(self, tool_name, level, summary, preview=None):
            prompted.append(tool_name)
            return "allow"

    registry = ToolRegistry(gate=RecordingGate())
    ctx = _rule_ctx(tmp_path, [PermissionRule("ask", "list_dir")])
    registry.bind(ctx)
    (tmp_path / "sub").mkdir()
    res = await registry.dispatch("list_dir", _json.dumps({"path": "sub"}))
    assert not res.is_error
    assert prompted == ["list_dir"], "an explicit ask rule must prompt, even for reads"
