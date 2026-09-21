"""Accessibility regressions (2026-09-14): plain REPL + reduce_motion.

`oaset --plain` is the screen-reader surface: same SessionHost runtime, but
append-only plain text with no ANSI movement. `[ui] reduce_motion = true`
freezes every spinner, the cursor blink and the palette caret.
"""

from __future__ import annotations

from oaset.config import default_config, load_config, save_config
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.repl import PlainGate, PlainRepl
from oaset.tui.app import OasetApp

# ---------------------------------------------------------------- reduce_motion

def test_reduce_motion_config_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("OASET_HOME", str(tmp_path))
    cfg = load_config()
    cfg.ui.reduce_motion = True
    save_config(cfg)
    assert "[ui]" in (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "reduce_motion" in (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert load_config().ui.reduce_motion is True
    assert default_config().ui.reduce_motion is False  # opt-in


async def test_reduce_motion_freezes_tui_motion(workspace):
    cfg = default_config()
    cfg.ui.reduce_motion = True
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        assert app.input_box.cursor_blink is False, "cursor must not blink"
        assert app.status_bar.reduce_motion is True
        app.status_bar.set_busy(True)
        app.status_bar._tick()
        first = app.status_bar.left_text
        app.status_bar._tick()
        app.status_bar._tick()
        assert app.status_bar.left_text == first, "busy glyph must be static"


# -------------------------------------------------------------------- plain gate

def _gate_answer(answer: str):
    return lambda prompt: answer


async def test_plain_gate_answer_mapping():
    assert await PlainGate(_gate_answer("y")).request("run_shell", "exec", "cmd") == "allow"
    assert await PlainGate(_gate_answer("a")).request("run_shell", "exec", "cmd") == "always"

    session_gate = PlainGate(_gate_answer("a"))
    await session_gate.request("run_shell", "exec", "cmd")
    assert await session_gate.request("run_shell", "exec", "cmd") == "allow", (
        "'always' covers the rest of the session without re-asking")

    def _eof(prompt):
        raise EOFError

    assert await PlainGate(_eof).request("run_shell", "exec", "cmd") == "deny", (
        "no interactive user: fail closed")


# -------------------------------------------------------------------- plain repl

async def test_plain_repl_streams_lines_and_persists(workspace, isolated_home):
    cfg = default_config()
    cfg.default_provider = "mock"
    cfg.default_model = "mock/mock-echo"
    (workspace / "a.txt").write_text("hello", encoding="utf-8")  # the tool reads this
    script = [
        MockTurn(content_chunks=["先看文件。\n"],
                 tool_calls=[MockToolCall("read_file", {"path": "a.txt"})]),
        MockTurn(content_chunks=["这是答案。"]),
    ]
    lines_out: list[str] = []
    answers = iter(["你好", "/nosuch", "/exit"])
    repl = PlainRepl(cfg, workspace, model_id="mock/mock-echo",
                     provider=MockProvider(script),
                     input_fn=lambda _p: next(answers),
                     write=lines_out.append)
    rc = await repl.run()
    out = "".join(lines_out)
    assert rc == 0
    assert "mock/mock-echo" in out            # banner names the model
    assert "先看文件。" in out                 # streamed text arrives verbatim
    assert "[工具] read_file" in out          # tool call is one readable line
    assert "✓ read_file" in out               # completion carries a text mark, not color
    assert "这是答案。" in out
    assert "行式模式不支持 /nosuch" in out     # unknown slash command explained
    # session persisted exactly like a TUI session
    from oaset.session import SessionStore

    metas = SessionStore().list_sessions(cwd=str(workspace))
    assert metas and metas[0].message_count >= 2


async def test_plain_repl_e2e_subprocess(tmp_path, monkeypatch):
    """`printf '/exit\\n' | oaset --plain --mock` must exit 0 immediately."""
    import subprocess
    import sys

    monkeypatch.setenv("OASET_HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    r = subprocess.run(
        [sys.executable, "-m", "oaset", "--plain", "--mock"],
        input="/exit\n", capture_output=True, text=True, timeout=120, cwd=str(ws),
    )
    assert r.returncode == 0, r.stderr[-500:]
    assert "\x1b[" not in r.stdout, "plain mode must emit no ANSI escapes"
