"""Conversation selection & copy (the "selection copy is not done" report).

Mouse drag over the conversation selects whole blocks (a message, an answer, a
tool card) with a visible highlight; Ctrl+Shift+C and `/copy selection` put that
on the clipboard. `/copy` also targets a specific artifact (last answer, all
tool outputs, the whole transcript) so selection is not the only route.

Block granularity is intentional: a Rich-rendered widget has no character grid
to select in, and "copy this answer / this tool output" is the real use.
"""

from __future__ import annotations

from oaset.config import default_config
from oaset.providers.mock import MockProvider, MockTurn
from oaset.tui import clipboard
from oaset.tui.app import OasetApp
from oaset.tui.widgets.chat import ChatLog


def make_app(workspace):
    return OasetApp(cfg=default_config(), cwd=workspace,
                    provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                    model_id="mock/mock-echo")


def _capture_clipboard(monkeypatch) -> dict:
    captured: dict = {}

    def fake_set(text: str):
        captured["text"] = text
        return None

    monkeypatch.setattr(clipboard, "set_text", fake_set)
    return captured


# ------------------------------------------------------------- block level

def _populate(chat: ChatLog):
    user = chat.add_user("explain the build")
    turn = chat.start_assistant()
    turn.push_content("the build is stamped by CI")
    turn.finish(model="mock/mock-echo")
    card = chat.add_tool_card("read_file", "path=README.md")
    card.raw_output = "# README\nbody line"
    return user, turn, card


async def test_blocks_expose_copy_text(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        user, turn, card = _populate(app.chat)
        await pilot.pause(0.2)
        assert user.copy_text() == "explain the build"      # no prompt glyph
        assert "stamped by CI" in turn.copy_text()
        assert "read_file" in card.copy_text() and "body line" in card.copy_text()


async def test_selection_spans_a_range_and_highlights(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        user, turn, card = _populate(app.chat)
        await pilot.pause(0.2)
        blocks = app.chat.selectable_blocks()
        assert [b for b in (user, turn, card) if b in blocks] == [user, turn, card]

        app.chat.begin_selection(user)
        app.chat.extend_selection(turn)
        assert app.chat.selected_blocks() == [user, turn]
        assert user.has_class("chat-selected") and turn.has_class("chat-selected")
        assert not card.has_class("chat-selected")

        text = app.chat.selected_text()
        assert "explain the build" in text and "stamped by CI" in text
        assert "\n\n" in text, "blocks are separated by a blank line"

        app.chat.clear_selection()
        assert app.chat.selected_text() == ""
        assert not user.has_class("chat-selected")


async def test_selection_is_ordered_and_reversible(workspace):
    """Dragging upward selects the same range (anchor stays the first block)."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        user, turn, card = _populate(app.chat)
        await pilot.pause(0.2)
        app.chat.begin_selection(card)
        app.chat.extend_selection(user)
        assert app.chat.selected_blocks() == [user, turn, card]


async def test_mouse_drag_selects_and_does_not_toggle_the_card(workspace):
    """A drag is a selection, not a click: the tool card must stay collapsed."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        _user, turn, card = _populate(app.chat)
        await pilot.pause(0.3)
        was_expanded = card.expanded

        # Pilot drags target widgets, which matches the block-level selection
        await pilot.mouse_down(turn)
        await pilot.hover(card)
        await pilot.pause(0.1)
        await pilot.mouse_up(card)
        await pilot.pause(0.2)

        assert app.chat.selected_blocks(), "the drag must produce a selection"
        assert card.expanded == was_expanded, "a selection drag must not expand the card"


# ------------------------------------------------------------ copy actions

async def test_ctrl_shift_c_copies_the_selection(workspace, monkeypatch):
    captured = _capture_clipboard(monkeypatch)
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        user, turn, _card = _populate(app.chat)
        await pilot.pause(0.2)
        app.chat.begin_selection(user)
        app.chat.extend_selection(turn)

        await app.action_copy_selection()
        assert "explain the build" in captured["text"]
        assert "stamped by CI" in captured["text"]


async def test_copy_falls_back_to_the_draft_when_nothing_is_selected(workspace, monkeypatch):
    captured = _capture_clipboard(monkeypatch)
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.input_area.load_text("draft text")
        await app.action_copy_selection()
        assert captured["text"] == "draft text"


async def test_copy_tools_and_all_targets(workspace, monkeypatch):
    captured = _capture_clipboard(monkeypatch)
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        _populate(app.chat)
        await pilot.pause(0.2)

        app.submit_text("/copy tools")
        await pilot.pause(0.3)
        assert "read_file" in captured["text"] and "body line" in captured["text"]

        app.submit_text("/copy all")
        await pilot.pause(0.3)
        assert "explain the build" in captured["text"]
        assert "read_file" in captured["text"]


async def test_copy_selection_with_nothing_selected_warns(workspace, monkeypatch):
    _capture_clipboard(monkeypatch)
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        _populate(app.chat)
        await pilot.pause(0.2)
        from oaset.tui.widgets.chat import NoticeCard

        app.submit_text("/copy selection")
        await pilot.pause(0.3)
        text = " ".join(n.raw_text for n in app.chat.query(NoticeCard))
        assert "尚未选中" in text or "nothing selected" in text, text


async def test_escape_clears_a_selection_before_interrupting(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        user, turn, _card = _populate(app.chat)
        await pilot.pause(0.2)
        app.chat.begin_selection(user)
        app.chat.extend_selection(turn)

        await app.action_interrupt()          # Escape
        await pilot.pause(0.1)
        assert app.chat.selected_text() == "", "Escape must clear the selection first"


async def test_right_click_copies_a_selection_cmd_style(workspace, monkeypatch):
    """cmd.exe convention: select, then right-click copies and clears."""
    captured = _capture_clipboard(monkeypatch)
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        user, turn, _card = _populate(app.chat)
        await pilot.pause(0.2)
        app.chat.begin_selection(user)
        app.chat.extend_selection(turn)

        # right-click anywhere over the conversation (button 3)
        await pilot.mouse_down(turn, button=3)
        await pilot.pause(0.1)
        await pilot.mouse_up(turn)
        await pilot.pause(0.2)

        assert "explain the build" in captured["text"], "right-click must copy"
        assert app.chat.selected_text() == "", "cmd clears the highlight after copy"


async def test_right_click_pastes_when_nothing_is_selected(workspace, monkeypatch):
    def fake_get():
        return ("pasted from clipboard", None)

    monkeypatch.setattr(clipboard, "get_text", fake_get)
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        # right-click over the conversation: nothing selected -> paste
        await pilot.mouse_down(app.chat, button=3)
        await pilot.pause(0.1)
        await pilot.mouse_up(app.chat)
        await pilot.pause(0.3)

        assert "pasted from clipboard" in app.input_area.text, \
            "right-click with no selection must paste at the input cursor"
        assert app.focused is app.input_area, \
            "right-click paste must restore the input so the next keystroke types there"


async def test_input_right_click_pastes(workspace, monkeypatch):
    def fake_get():
        return ("typed via cmd paste", None)

    monkeypatch.setattr(clipboard, "get_text", fake_get)
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.mouse_down(app.input_area, button=3)
        await pilot.pause(0.1)
        await pilot.mouse_up(app.input_area)
        await pilot.pause(0.2)
        assert "typed via cmd paste" in app.input_area.text
        assert app.focused is app.input_area


def test_chat_css_has_no_block_background():
    from oaset.tui.app import OasetApp
    from oaset.tui.widgets.chat import ChatLog
    from oaset.tui.widgets.inline import InlinePromptBase
    from oaset.tui.widgets.input import InputArea
    from oaset.tui.widgets.status_bar import StatusBar
    from oaset.tui.widgets.tool_card import ToolCard, ToolGroupCard

    css = "\n".join((
        OasetApp.CSS,
        ChatLog.DEFAULT_CSS,
        InputArea.DEFAULT_CSS,
        InlinePromptBase.DEFAULT_CSS,
        StatusBar.DEFAULT_CSS,
        ToolCard.DEFAULT_CSS,
        ToolGroupCard.DEFAULT_CSS,
    ))
    assert "background: $surface" not in css
    assert "background: transparent" not in css, (
        "CSS transparent becomes #000000 in Textual; use ansi_default")
    assert "[reverse]" not in css
    assert "background: ansi_default" in OasetApp.CSS
    assert ".chat-selected { text-style: underline; }" in ChatLog.DEFAULT_CSS


def test_picker_and_suggest_highlight_without_reverse_fill():
    from pathlib import Path

    from oaset.tui.controllers import prompts
    from oaset.tui.widgets import inline, palette

    for path in (
        Path(inline.__file__),
        Path(palette.__file__),
        Path(prompts.__file__),
        Path("src/oaset/tui/app.py"),
    ):
        text = path.read_text(encoding="utf-8")
        assert "[reverse]" not in text, f"{path} still paints reverse-video fill"


async def test_live_widgets_use_terminal_default_background(workspace):
    """The painted Screen/chat/input/status must not be a solid RGB fill."""
    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        assert app.ansi_color is True
        for widget in (app.screen, app.chat, app.input_area, app.status_widget):
            color = widget.styles.background
            assert color.ansi == -1, (
                f"{widget} background={color!r} still paints a fill instead of "
                "the terminal default"
            )
        strip = app.screen._compositor.render_strips()[0]
        backgrounds = {getattr(seg.style, "bgcolor", None) for seg in strip if seg.style}
        painted = {
            bg for bg in backgrounds
            if bg is not None and getattr(bg, "number", None) is None
            and getattr(bg, "triplet", None) is not None
        }
        assert not painted, f"compositor still paints RGB fills: {painted}"
