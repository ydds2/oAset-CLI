"""P1 interaction quick-win regressions (docs/PLAN.zh-CN.md §3): scrollbars
and position feedback, auto-growing input, keybinding governance, the queue
semantics, tool-card detail, and the localised error/i18n channel."""

from __future__ import annotations

from oaset.config import default_config
from oaset.i18n import CATALOG
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.tui.app import OasetApp
from oaset.tui.commands import COMMANDS, RISK_DANGER
from oaset.tui.keymap import KEYMAP
from oaset.tui.widgets.tool_card import ToolCard


def make_app(workspace, script=None):
    provider = MockProvider(script or [])
    return OasetApp(cfg=default_config(), cwd=workspace, provider=provider,
                    model_id="mock/mock-echo")


# ----------------------------------------------------- P1-3 keymap governance


def test_keymap_is_the_single_source():
    from oaset.tui.keymap import shortcut_rows

    rows = shortcut_rows()
    assert len(rows) == len(KEYMAP)
    for keys, key, _fallback in KEYMAP:
        assert key in CATALOG and CATALOG[key].get("zh"), f"{key} missing zh text"


def test_ctrl_x_is_cut_not_interrupt():
    assert all(b.key != "ctrl+x" for b in OasetApp.BINDINGS), \
        "ctrl+x belongs to the input box cut semantics; interrupt is Escape-only"
    assert any(b.key == "escape" and b.action == "interrupt" for b in OasetApp.BINDINGS)


def test_every_builtin_command_is_localised():
    for cmd in COMMANDS:
        key = "cmd_" + cmd.name.replace("-", "_")
        assert key in CATALOG and CATALOG[key].get("zh"), f"/{cmd.name} lacks cmd_* text"


def test_exit_confirms_like_other_destructive_commands():
    exit_cmd = next(c for c in COMMANDS if c.name == "exit")
    assert exit_cmd.risk == RISK_DANGER and exit_cmd.needs_confirmation
    assert exit_cmd.effect, "destructive commands must declare their effect"


def test_help_renders_localised_descriptions_and_keymap():
    from oaset.i18n import set_language
    from oaset.tui.widgets.inline import help_text

    set_language("zh")
    text = help_text()
    assert "退出 oAset" in text, "/help must localise command descriptions"
    assert "Ctrl+X" in text and "剪切" in text
    set_language("en")
    assert "cut in the input box" in help_text()
    set_language("zh")


def test_permission_badges_are_localised():
    from oaset.i18n import set_language, t
    from oaset.tui.widgets.inline import InlinePermission

    set_language("zh")
    perm = InlinePermission("run_shell", "exec", "do things")

    class _Body:
        text = ""

        def update(self, text_: str) -> None:
            self.text = text_

    perm._body = _Body()
    perm._refresh()
    assert "执行" in perm._body.text
    assert t("badge_write") == "写入"
    set_language("zh")


# ------------------------------------------------------- P1-2 input autosize


async def test_input_area_grows_and_reports_position(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        draft = "\n".join(f"line {i}" for i in range(1, 13))
        app.input_area.load_text(draft)
        await pilot.pause(0.2)
        height = app.input_area.styles.height.value
        assert height and height > 4, "the input must grow past the old fixed 4 rows"
        assert app.status_bar._input_total == 12
        app.input_area.load_text("one")
        await pilot.pause(0.2)
        assert (app.input_area.styles.height.value or 1) <= 2, "it must shrink back"


# ------------------------------------------------------ P1-1 scroll feedback


async def test_scroll_position_is_reported(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.1)
        app.chat.add_card("\n".join(f"row {i}" for i in range(60)))
        await pilot.pause(0.3)
        app.chat.scroll_home(animate=False)
        await pilot.pause(0.3)
        pct = app.status_bar._scroll_pct
        assert pct is not None and pct < 0.5, \
            "scrolling up must surface a position indicator"


# ---------------------------------------------------------- P1-6 the queue


async def test_dequeue_clears_the_queue(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app.input_queue.extend(["a", "b"])
        app.submit_text("/dequeue")
        await pilot.pause(0.3)
        assert app.input_queue == []


async def test_exit_asks_for_confirmation(workspace):
    from oaset.tui.widgets.palette import InlineCommandConfirm

    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/exit")
        await pilot.pause(0.3)
        assert list(app.query(InlineCommandConfirm)), \
            "/exit is danger-risk: it must confirm before exiting"


# ------------------------------------------------------- P1-7 tool card detail


async def test_tool_card_shows_duration_and_full_args(workspace):
    script = [
        MockTurn(content_chunks=["working"],
                 tool_calls=[MockToolCall("read_file", {"path": "deep/dir/file.txt"})]),
        MockTurn(content_chunks=["done"]),
    ]
    (workspace / "file.txt").write_text("data", encoding="utf-8")
    app = make_app(workspace, script)
    async with app.run_test(size=(120, 40)) as pilot:
        app.input_area.load_text("go")
        await pilot.press("enter")
        await pilot.pause(0.5)
        cards = list(app.chat.query(ToolCard))
        assert cards, "the tool call must render a card"
        card = cards[0]
        assert card._elapsed is not None, "each card reports its own duration"
        assert "file.txt" in card.raw_args, "the full arguments stay on the card"
        body = card._render_body()  # what toggle_expand feeds the body Static
        renderables = getattr(body, "renderables", None)
        assert renderables, "the expanded body groups the args line above the output"
        args_line = str(renderables[0])
        assert "args:" in args_line and "file.txt" in args_line


# -------------------------------------------------- P1-4 error surfacing


async def test_error_pins_headline_and_hint(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app.notify_error(RuntimeError("boom"), code="test.boom",
                         hint="try again", source="test")
        await pilot.pause(0.1)
        assert app.status_bar._error
        assert app.status_bar._error_hint == "try again", \
            "the recovery hint stays visible on row 2 while the error is pinned"
