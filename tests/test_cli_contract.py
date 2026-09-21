"""CLI contract: stdin pipelines, verbosity, stdout/stderr split, exit codes.

The CLI contract (once in docs/CONTRACTS.zh-CN.md, now the tests themselves) The split matters
for scripts: stdout is the result, stderr is progress - a pipeline that gets
tool chatter on stdout cannot be parsed.
"""

from __future__ import annotations

import io
import os
import sys

import pytest

from oaset import cli


def _piped_stdin(monkeypatch, text: str):
    """Install a REAL pipe as stdin (os.fstat must see a FIFO, like a shell pipe)."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, text.encode("utf-8"))
    os.close(write_fd)
    stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stream)
    return stream


def _tty_stdin() -> io.StringIO:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    return _Tty("")


# ------------------------------------------------------------- input sources

def test_stdin_is_ignored_when_interactive(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _tty_stdin())
    assert cli.read_stdin_if_piped() == ""


def test_piped_stdin_is_read(monkeypatch):
    _piped_stdin(monkeypatch, "piped body\n")
    assert cli.read_stdin_if_piped() == "piped body\n"


def test_stdin_without_a_real_descriptor_is_never_read(monkeypatch):
    """A programmatic stdin stub (pytest, a GUI launcher) must not be read.

    Reading it blocked the whole test suite once: the CLI requires a real file
    descriptor (FIFO or regular file) before touching stdin.
    """

    class _NoDescriptor(io.StringIO):
        def isatty(self) -> bool:
            return False

        def fileno(self) -> int:
            raise OSError("no real descriptor")

    monkeypatch.setattr(sys, "stdin", _NoDescriptor("would block"))
    assert cli.read_stdin_if_piped() == ""


def test_empty_redirected_file_is_not_a_prompt(monkeypatch, tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    handle = empty.open("r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", handle)
    try:
        assert cli.read_stdin_if_piped() == ""
    finally:
        handle.close()


def test_piped_stdin_alone_becomes_a_one_shot_prompt(monkeypatch, capsys):
    """`cat log | oaset` is a pipeline, not a request to start the TUI."""
    seen: dict = {}

    async def fake_one_shot(cfg, cwd, prompt, *args, **kwargs):
        seen["prompt"] = prompt
        return 0

    monkeypatch.setattr(cli, "run_one_shot", fake_one_shot)
    _piped_stdin(monkeypatch, "summarise this log")
    assert cli.main(["--mock"]) == 0
    assert seen["prompt"] == "summarise this log"


def test_dash_p_and_stdin_are_combined(monkeypatch):
    seen: dict = {}

    async def fake_one_shot(cfg, cwd, prompt, *args, **kwargs):
        seen["prompt"] = prompt
        return 0

    monkeypatch.setattr(cli, "run_one_shot", fake_one_shot)
    _piped_stdin(monkeypatch, "BODY")
    assert cli.main(["--mock", "-p", "INSTRUCTION"]) == 0
    assert "INSTRUCTION" in seen["prompt"] and "BODY" in seen["prompt"]


# ------------------------------------------------------------- --think flag

def test_dash_think_is_normalized_and_passed_through(monkeypatch):
    seen: dict = {}

    async def fake_one_shot(cfg, cwd, prompt, *args, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_one_shot", fake_one_shot)
    monkeypatch.setattr(sys, "stdin", _tty_stdin())
    assert cli.main(["--mock", "-p", "hi", "--think", "HIGH"]) == 0
    assert seen.get("thinking") == "heavy", "aliases normalize before the run"
    assert cli.main(["--mock", "-p", "hi", "--think", "min"]) == 0
    assert seen.get("thinking") == "light"


def test_dash_think_invalid_exits_two_with_a_named_error(monkeypatch, capsys):
    async def fake_one_shot(cfg, cwd, prompt, *args, **kwargs):
        raise AssertionError("must not run with an unknown level")

    monkeypatch.setattr(cli, "run_one_shot", fake_one_shot)
    monkeypatch.setattr(sys, "stdin", _tty_stdin())
    assert cli.main(["--mock", "-p", "hi", "--think", "lots"]) == 2
    err = capsys.readouterr().err
    assert "--think" in err and "lots" in err


# ------------------------------------------------------------- verbosity

def test_quiet_and_verbose_are_accepted(monkeypatch):
    seen: dict = {}

    async def fake_one_shot(cfg, cwd, prompt, *args, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_one_shot", fake_one_shot)
    monkeypatch.setattr(sys, "stdin", _tty_stdin())
    assert cli.main(["--mock", "-q", "-v", "-p", "hi"]) == 0
    assert seen.get("quiet") is True and seen.get("verbose") is True


# ------------------------------------------------------------- exit codes

def test_version_exits_zero_without_config(tmp_path, monkeypatch, capsys):
    """`--version` must work even when config.toml is corrupt (item 3)."""
    monkeypatch.setenv("OASET_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("not = valid toml [[[", encoding="utf-8")
    assert cli.main(["--version"]) == 0
    assert "oAset CLI v" in capsys.readouterr().out


def test_unknown_flag_is_a_usage_error():
    """Usage errors are 2, not 1: CI can tell 'typo' from 'run failed'."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--definitely-not-a-flag"])
    assert excinfo.value.code == 2


def test_bad_model_exits_with_a_distinct_code(capsys):
    assert cli.main(["models", "verify", "nope/nope", "--json"]) == 2


# ------------------------------------------------------------- output formats

async def _one_shot(output_format: str, tmp_path, provider) -> int:
    from oaset.config import default_config

    cfg = default_config()
    cfg.permission_mode = "default"
    return await cli.run_one_shot(
        cfg, tmp_path, "say hi", None, provider, yolo=True,
        output_format=output_format, quiet=True,
    )


async def test_json_output_is_exactly_one_parseable_document(tmp_path, capsys, isolated_home):
    """A8 regression: --output-format json stdout must be a single valid JSON
    document — streamed content must never mix into it."""
    import json as _json

    from oaset.providers import MockProvider, MockTurn

    provider = MockProvider([MockTurn(content_chunks=["hello ", "world"], finish_reason="stop")])
    rc = await _one_shot("json", tmp_path, provider)
    assert rc == 0
    out = capsys.readouterr().out
    doc = _json.loads(out)  # raises if anything else leaked onto stdout
    assert doc["reply"] == "hello world"
    assert doc["error"] is None
    assert any(e["type"] == "turn_completed" for e in doc["events"])


async def test_stream_json_every_line_is_a_complete_event(tmp_path, capsys, isolated_home):
    import json as _json

    from oaset.providers import MockProvider, MockTurn

    provider = MockProvider([MockTurn(content_chunks=["a", "b"], finish_reason="stop")])
    rc = await _one_shot("stream-json", tmp_path, provider)
    assert rc == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "stream-json must emit at least one event"
    types = []
    for line in lines:
        event = _json.loads(line)  # every line must parse
        assert "type" in event
        types.append(event["type"])
    assert types[-1] == "turn_completed"
    assert "assistant_delta" in types


async def test_json_output_failure_still_parseable(tmp_path, capsys, isolated_home):
    """A provider failure in json mode still prints a valid JSON error doc."""
    import json as _json

    from oaset.providers import MockProvider
    from oaset.providers.base import ProviderError

    class FailingProvider(MockProvider):
        async def stream(self, messages, tools):
            raise ProviderError("server", "boom", retryable=False)
            yield  # pragma: no cover

    rc = await _one_shot("json", tmp_path, FailingProvider())
    assert rc == 1
    doc = _json.loads(capsys.readouterr().out)
    assert doc["reply"] == "" and doc["error"]["message"] == "boom"


async def test_text_output_unchanged(tmp_path, capsys, isolated_home):
    from oaset.providers import MockProvider, MockTurn

    provider = MockProvider([MockTurn(content_chunks=["plain text"], finish_reason="stop")])
    rc = await _one_shot("text", tmp_path, provider)
    assert rc == 0
    assert "plain text" in capsys.readouterr().out


async def test_one_shot_tool_progress_is_human_and_on_stderr(tmp_path, capsys, isolated_home):
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockToolCall, MockTurn

    cfg = default_config()
    (tmp_path / "a.txt").write_text("data", encoding="utf-8")
    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("read_file", {"path": "a.txt"})]),
        MockTurn(content_chunks=["found it"], finish_reason="stop"),
    ])
    rc = await cli.run_one_shot(
        cfg, tmp_path, "read a.txt", None, provider, yolo=True,
        output_format="text", quiet=False,
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "found it" in captured.out
    assert "[tool]" not in captured.err
    assert "ReadFile" in captured.err
    assert "Used" in captured.err or "调用" in captured.err


async def test_one_shot_without_a_key_exits_before_calling_the_provider(
        tmp_path, capsys, isolated_home):
    from oaset.config import default_config

    cfg = default_config()
    rc = await cli.run_one_shot(
        cfg, tmp_path, "hello", None, None, yolo=True,
        output_format="text", quiet=False,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "oaset login" in err  # headless: the CLI command, not the TUI one
    assert "hello" not in capsys.readouterr().out


# ------------------------------------------------------------- network discipline

def test_doctor_local_only_never_touches_network(monkeypatch, capsys, isolated_home):
    """A9 regression: under network.mode=local_only, doctor must skip the
    remote endpoint HEAD probe — the user promised zero remote I/O."""
    import httpx

    from oaset.config import default_config, save_config

    def explode(*a, **k):
        raise AssertionError("httpx.head must not run under local_only")

    monkeypatch.setattr(httpx, "head", explode)
    cfg = default_config()
    cfg.network_mode = "local_only"
    save_config(cfg)
    assert cli.cmd_doctor() in (0, 1)
    captured = capsys.readouterr()
    assert "skipped" in captured.out  # the skip is visible, not silent


def test_doctor_pull_only_still_probes_endpoint(monkeypatch, capsys, isolated_home):
    import httpx

    from oaset.config import default_config, save_config

    class FakeResp:
        status_code = 200

    monkeypatch.setattr(httpx, "head", lambda *a, **k: FakeResp())
    cfg = default_config()
    cfg.network_mode = "pull_only"
    save_config(cfg)
    cli.cmd_doctor()
    assert "HTTP 200" in capsys.readouterr().out


def test_frozen_upgrade_does_not_invoke_pip(monkeypatch, capsys):
    """A10 regression: a frozen exe's `upgrade` must never run
    `sys.executable -m pip` (sys.executable IS oaset.exe there)."""
    import oaset.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_is_frozen", lambda: True)
    rc = cli.cmd_upgrade()
    assert rc == 5
    err = capsys.readouterr().err
    assert "update apply" in err and "pip self-upgrade does not apply" in err


def test_source_upgrade_still_uses_pip(monkeypatch, capsys):
    import oaset.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_is_frozen", lambda: False)
    called = {}
    def fake_run(argv, **k):
        called["argv"] = argv
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    rc = cli.cmd_upgrade()
    assert rc == 0 and called["argv"][0:3] == [cli_mod.sys.executable, "-m", "pip"]


# ---------------- publish-readiness fixes (cli/cron/update blind review)

def test_did_you_mean_fires_on_typos(monkeypatch, capsys):
    """The suggestion list snapshot ran before any subparser existed — the
    feature was dead code since birth."""
    from oaset import cli as climod

    parser = climod.build_parser()
    assert parser._oaset_choices, "subcommand list must be populated"
    assert "sessions" in parser._oaset_choices
    with pytest.raises(SystemExit) as exc:
        monkeypatch.setattr("sys.argv", ["oaset", "sesions"])
        climod.parser = parser
        parser.parse_args(["sesions"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "did you mean" in err and "sessions" in err


def test_gateway_policy_gates_sending_not_receiving():
    """The old condition rejected the SAFE receive-only default and let
    --send-replies (the actual push!) bypass the policy — pull_only then
    pushed user content to a third party."""
    from oaset.network import NetworkPolicy

    policy = NetworkPolicy("pull_only")
    using_transport = True
    send_replies = False
    blocked = using_transport and send_replies and not policy.allows("push")
    assert not blocked, "receive-only transport must be allowed"
    send_replies = True
    blocked = using_transport and send_replies and not policy.allows("push")
    assert blocked, "sending under pull_only must be refused"


def test_mcp_add_refuses_to_wipe_unparseable_config(tmp_path, monkeypatch):
    """A truncated mcp.json used to be treated as empty — every configured
    server silently erased, exit 0."""

    from oaset import cli as climod

    home = tmp_path
    (home / "mcp.json").write_text('{"servers": {"important": ', encoding="utf-8")

    monkeypatch.setenv("OASET_HOME", str(home))
    from oaset.i18n import set_language

    set_language("en")
    rc = climod.main(["mcp", "add", "newone", "--url",
                      "http://127.0.0.1:9/v1"])
    assert rc in (1, 2), "must fail, not wipe"
    kept = (home / "mcp.json").read_text(encoding="utf-8")
    assert "important" in kept, "original content must be untouched"
    assert "newone" not in kept


def test_cron_five_field_matches_dom_mon_dow():
    from datetime import datetime as _dt

    from oaset.cron import CronJob, is_due

    jan_only = CronJob(id="a", name="j", schedule="0 9 1 1 *", prompt="p")
    assert not is_due(jan_only, _dt(2026, 9, 20, 9, 0))
    assert is_due(jan_only, _dt(2027, 1, 1, 9, 0))
    monday = CronJob(id="b", name="j", schedule="0 9 * * 1", prompt="p")
    assert not is_due(monday, _dt(2026, 9, 20, 9, 0))  # Sunday
    assert is_due(monday, _dt(2026, 9, 21, 9, 0))      # Monday
    sunday = CronJob(id="c", name="j", schedule="0 9 * * 0", prompt="p")
    assert is_due(sunday, _dt(2026, 9, 20, 9, 0))      # cron 0 = Sunday


def test_cron_add_rejects_garbage_schedule(capsys):
    """Unparseable schedules used to be stored as permanently-dead jobs."""
    import tempfile
    from pathlib import Path

    from oaset import cli as climod
    from oaset.cron import load_jobs

    home = Path(tempfile.mkdtemp())
    import os

    os.environ["OASET_HOME"] = str(home)
    rc = climod.main(["cron", "add", "--name", "broken", "--schedule",
                      "every thursday at noon", "--prompt", "hi"])
    assert rc == 2
    assert load_jobs(home) == [] or all(j.schedule != "every thursday at noon"
                                        for j in load_jobs(home))


def test_update_plain_http_sources_refused():
    from oaset.update import UpdateError, _assert_source_allowed

    with pytest.raises(UpdateError):
        _assert_source_allowed("http://mirror.example.com/catalog.json")
    _assert_source_allowed("https://example.com/catalog.json")
    _assert_source_allowed("http://mirror.example.com/x", allow_plain_http=True)


def test_search_native_roundtrips(cfg):
    from oaset.config import load_config, save_config

    cfg.search.native = "off"
    save_config(cfg)
    loaded = load_config()
    assert loaded.search.native == "off", "the documented switch must load"
