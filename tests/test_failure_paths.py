"""Fourth-round audit regressions (2026-09-16): failure paths must do what
the success paths do.

Pins: error-interrupted turns seal, duplicate tool ids and denied calls
finish exactly once, recalled commands submit instead of accepting a
suggestion, markup survives status-bar clipping, negative budgets are
refused, MCP argv quoting works, and worktree names are validated.
"""

from __future__ import annotations

from oaset.config import default_config
from oaset.providers import MockProvider
from oaset.tui.app import OasetApp


def test_status_bar_clip_never_breaks_markup():
    from rich.text import Text

    from oaset.tui.widgets.status_bar import clip_markup

    text = "x [yellow]stalled 12s[/yellow] [bold yellow]auto[/bold yellow] tail"
    for width in (1, 5, 12, 20, 45, 200):
        clipped = clip_markup(text, width)
        Text.from_markup(clipped)  # raises MarkupError on unbalanced markup


def test_mcp_arg_split_keeps_quoted_paths_whole():
    from oaset.tui.controllers.mcp_admin_ui import _parse_tool_filter, _split_args

    parts = _split_args('--config "C:\\Program Files\\app\\cfg.json" --verbose')
    assert parts == ["--config", "C:\\Program Files\\app\\cfg.json", "--verbose"]

    plus, minus = _parse_tool_filter("read_file +grep -write_file")
    # bare token = enable: it used to be dropped while the save said "saved"
    assert plus == ["read_file", "grep"]
    assert minus == ["write_file"]


async def test_error_interrupted_turn_still_seals(workspace):
    """A provider failure mid-stream must finish the reply block — the old
    path left the block streaming (and the flusher spinning) forever."""
    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)

        async def failing_chat(*a, **k):
            from oaset.events import Event

            yield Event(schema_version=1, event_id="e1", session_id="s",
                        sequence=1, timestamp="t", type="turn_started", data={})
            raise RuntimeError("provider exploded mid-stream")

        app.host.chat = failing_chat
        app.submit_text("hello")
        for _ in range(120):
            await pilot.pause(0.05)
            if app.flusher_turns and all(turn._done for turn in app.flusher_turns):
                break
        # the list prunes lazily on the NEXT submit; the contract is that a
        # dead turn is DONE (sealed), so the 120ms flusher never touches it
        assert app.flusher_turns and all(turn._done for turn in app.flusher_turns),             "an error-interrupted turn must be sealed"
        assert app.status_bar._busy is False
        assert app.is_running


async def test_duplicate_tool_id_finishes_the_old_card(workspace):
    from oaset.tui.turn_view import handle_turn_event

    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        turn = app.chat.start_assistant()
        cards: dict = {}
        handle_turn_event(app, {"type": "tool_call_started",
                                "id": "dup", "name": "read_file", "arguments": "{}"}, turn, cards)
        first = app.tool_runs["dup"]
        handle_turn_event(app, {"type": "tool_call_started",
                                "id": "dup", "name": "read_file", "arguments": "{}"}, turn, cards)
        assert first.elapsed is not None, "orphaned run must be finished"
        assert first.is_error
        assert app.tool_runs["dup"] is not first


async def test_denied_tool_card_finishes_once(workspace):
    """denial emits tool_denied AND tool_end — the second finish used to
    wrap the title into itself."""
    from oaset.tui.turn_view import handle_turn_event

    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        turn = app.chat.start_assistant()
        cards: dict = {}
        handle_turn_event(app, {"type": "tool_call_started",
                                "id": "d1", "name": "run_shell", "arguments": "{}"}, turn, cards)
        handle_turn_event(app, {"type": "tool_denied", "id": "d1",
                                "reason": "user denied"}, turn, cards)
        title_after_deny = app.tool_runs["d1"].card.title_text
        handle_turn_event(app, {"type": "tool_end", "id": "d1",
                                "result": "[denied]", "is_error": True}, turn, cards)
        assert app.tool_runs["d1"].card.title_text == title_after_deny, \
            "the tool_end after tool_denied must not re-finish the card"


async def test_recalled_slash_command_submits_on_next_enter(workspace):
    """↑ recall puts a command in the box; Enter must SEND it, not accept a
    suggestion (possibly a different command)."""
    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app.input_area.send_history = ["/model", "/help"]
        app.input_area.load_text("")
        await pilot.press("up")  # recall /help
        await pilot.pause(0.2)
        assert app.input_area.text == "/help"
        assert not app.input_area._suggest_visible, "no popup on recalled text"
        await pilot.press("enter")
        for _ in range(120):
            await pilot.pause(0.05)
            if list(app.chat.query(".chat-card")):
                break
        assert list(app.chat.query(".chat-card")), "/help must have run"


async def test_negative_goal_budget_is_refused(workspace):
    """`/goal budget -5` used to lock the session into permanent
    'budget exhausted' from the next turn on."""
    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app.submit_text("/goal watch the tests")
        await pilot.pause(0.4)
        app.submit_text("/goal budget -5")
        await pilot.pause(0.4)
        assert app._kernel.budget_tokens != -5000, "negative budget must not land"
        assert app._kernel.budget_tokens in (0, None)


def test_worktree_remove_rejects_path_separators(tmp_path):
    from oaset.worktree import WorktreeError, remove_worktree

    try:
        remove_worktree(tmp_path, "../..")
    except WorktreeError as exc:
        assert "separators" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("path-separator names must be rejected")


def test_usage_by_days_zero_days_is_empty(isolated_home):
    from oaset.session import SessionStore

    # days<=0 must be an empty window, not every day ever recorded
    assert SessionStore().usage_by_days(0) == []
    assert SessionStore().usage_by_days(-1) == []
