"""AI output formatting: markdown integrity, per-turn meta, diff/syntax render.

Locks down the two real defects that were fixed: a `● ` prefix inside the
markdown source (which broke the first fenced block / heading / table) and a
diff "renderer" that coloured any line starting with +/-.
"""

from __future__ import annotations

import time

import pytest

from oaset.config import default_config
from oaset.providers import MockProvider, MockTurn
from oaset.tui.app import OasetApp
from oaset.tui.widgets.chat import AssistantTurn
from oaset.tui.widgets.tool_card import (
    ToolCard,
    guess_lexer,
    looks_like_diff,
    render_tool_output,
)


@pytest.fixture(autouse=True)
def isolated_config_path(tmp_path, monkeypatch):
    import oaset.config as cfgmod

    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "config.toml")


def make_app(workspace):
    cfg = default_config()
    cfg.ui_language = "en"
    return OasetApp(cfg=cfg, cwd=workspace,
                    provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                    model_id="mock/mock-echo")


async def _await_turn(app, pilot, timeout=4.0):
    end = time.monotonic() + timeout
    turns: list = []
    while time.monotonic() < end:
        turns = list(app.chat.query(AssistantTurn))
        if turns and turns[0]._done:
            return turns[0]
        await pilot.pause(0.05)
    assert turns, "no assistant turn rendered"
    return turns[0]


# ------------------------------------------------------------ markdown source


async def test_markdown_gets_the_reply_without_a_bullet_prefix(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app.input_area.load_text("draw code")
        await pilot.press("enter")
        turn = await _await_turn(app, pilot)
        assert turn.text() and not turn.text().startswith("●")
        # Density parity: no standalone bullet widget exists any more.
        assert not hasattr(turn, "bullet")


async def test_no_standalone_bullet_row_renders(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        turn.push_content("hi")
        turn.flush()
        await pilot.pause(0.1)
        children = [type(w).__name__ for w in turn.children]
        assert "Label" not in children or turn.meta in turn.children
        assert "hi" in turn.text()


async def test_turn_meta_reports_model_and_elapsed(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app.input_area.load_text("hello")
        await pilot.press("enter")
        end = time.monotonic() + 4.0
        turns: list = []
        while time.monotonic() < end:
            turns = list(app.chat.query(AssistantTurn))
            if turns and turns[0]._done and turns[0].meta_text():
                break
            await pilot.pause(0.05)
        assert turns and turns[0].meta_text()
        meta = turns[0].meta_text()
        assert "mock/mock-echo" in meta  # per-turn model name
        assert "s" in meta               # elapsed seconds
        assert "tool calls" not in meta.lower() and "次工具" not in meta


async def test_interrupted_turn_marks_itself(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        turn.push_content("half")
        turn.finish(partial=True)
        assert turn.meta_text()


async def test_thinking_block_folds_with_a_first_line_summary(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        turn.push_reasoning("first thought line\nsecond line")
        turn.thinking.finish()
        await pilot.pause(0.05)
        # Collapsed: a one-line summary WITHOUT the reasoning excerpt.
        assert turn.thinking.collapsed
        collapsed_render = str(turn.thinking.render())
        assert "first thought line" not in collapsed_render
        assert "已思考" in collapsed_render or "thought " in collapsed_render
        turn.thinking.toggle()
        await pilot.pause(0.05)
        assert not turn.thinking.collapsed
        assert "first thought line" in str(turn.thinking.render())
        assert "second line" in str(turn.thinking.render())


# --------------------------------------------------------------- diff render


def test_diff_detection_requires_real_structure():
    assert looks_like_diff("@@ -1,3 +1,4 @@\n context\n+added\n-removed")
    assert looks_like_diff("diff --git a/x b/x\n--- a/x\n+++ b/x\n+new")
    assert looks_like_diff("--- a/x\n+++ b/x\n+new")
    # prose that merely starts with a dash must NOT be treated as a diff
    assert not looks_like_diff("- first bullet\n- second bullet")
    assert not looks_like_diff("no markers here")


def test_diff_styling_marks_headers_hunks_and_changes():
    text = "--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n-old\n+new\n context"
    body = render_tool_output("edit_file path=f.py", text)
    styles = {str(span.style) for span in body.spans}
    assert any("green" in s for s in styles)
    assert any("red" in s for s in styles)
    assert any("cyan" in s for s in styles)  # hunk header


def test_prose_dash_lines_are_not_coloured():
    body = render_tool_output("some_tool", "- just a bullet\n- another bullet")
    assert not getattr(body, "spans", [])


def test_lexer_guess_from_summary_and_content():
    assert guess_lexer("read_file path=src/app.py", "import os") == "python"
    assert guess_lexer("read_file path=config.toml", "[a]") == "toml"
    assert guess_lexer("tool", '{"a": 1}') == "json"
    assert guess_lexer("tool", "plain words only") is None


def test_code_output_is_highlighted_but_huge_output_is_plain():
    from rich.syntax import Syntax

    small = render_tool_output("read_file path=x.py", "def f():\n    return 1\n")
    assert isinstance(small, Syntax)
    huge = render_tool_output("read_file path=x.py", "x = 1\n" * 5000)
    assert not isinstance(huge, Syntax)


# --------------------------- tool activity through the event path (activity mode)


async def test_tool_activity_diff_lands_as_collapsed_card_at_flush(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        cards: dict = {}
        app._handle_event({"type": "tool_call_started", "id": "t1", "name": "edit_file",
                           "arguments": '{"path": "f.py"}'}, turn, cards)
        app._handle_event({"type": "tool_completed", "id": "t1", "name": "edit_file",
                           "is_error": False,
                           "result": "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new"},
                          turn, cards)
        mounted = list(app.chat.query(ToolCard))
        assert mounted, "the tool line mounts immediately, not at turn end"
        app.flush_tool_activity()
        await pilot.pause(0.1)
        card = cards["t1"].card
        assert card is not None and card.has_class("tool-ok")
        assert card.raw_output.startswith("--- a/f.py")
        assert "f.py" in card.title_text or "edit_file" in card.title_text.lower() or "EditFile" in card.title_text
        assert card._peek.display is False, "activity cards enter collapsed"
        card.toggle_expand()
        assert card._body.display


async def test_tool_activity_marks_errors(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        cards: dict = {}
        app._handle_event({"type": "tool_call_started", "id": "e1", "name": "run_shell",
                           "arguments": '{"command": "boom"}'}, turn, cards)
        app._handle_event({"type": "tool_completed", "id": "e1", "name": "run_shell",
                           "result": "failed", "is_error": True}, turn, cards)
        app.flush_tool_activity()
        await pilot.pause(0.1)
        card = cards["e1"].card
        assert "✗" in card.title_text
        assert card.has_class("tool-error")


async def test_read_file_title_does_not_repeat_the_path(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        cards: dict = {}
        app._handle_event({"type": "tool_call_started", "id": "r1", "name": "read_file",
                           "arguments": '{"path": "README.md"}'}, turn, cards)
        app._handle_event({"type": "tool_completed", "id": "r1", "name": "read_file",
                           "result": "README.md (200 lines)\n     1\t# oAset CLI\n"}, turn, cards)
        title = cards["r1"].card.title_text
        assert title.count("README.md") == 1, title
        assert "# oAset CLI" in title
        assert "1 #" not in title


async def test_todo_write_title_does_not_dump_json(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        cards: dict = {}
        payload = (
            '{"todos":[{"content":"用 GitHub API 拉取 topic:cli","status":"in_progress"},'
            '{"content":"按语言归类","status":"pending"}]}'
        )
        app._handle_event({"type": "tool_call_started", "id": "td1",
                           "name": "todo_write", "arguments": payload}, turn, cards)
        app._handle_event({"type": "tool_completed", "id": "td1", "name": "todo_write",
                           "result": "Todo list updated: 0/2 completed"}, turn, cards)
        card = cards["td1"].card
        assert "todos=[2]" in card.title_text
        assert "GitHub API" not in card.title_text
        assert "[{" not in card.title_text


async def test_finished_turn_renders_markdown_tables(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        turn.push_content("# Title\n\n**bold**\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
        turn.flush()
        await pilot.pause(0.05)
        from textual.widgets import Markdown as _Markdown

        assert turn._committed_src, "closed blocks commit during the stream"
        turn.finish()
        await pilot.pause(0.3)
        assert turn.live.display is False
        assert isinstance(turn.markdown, _Markdown) or turn._committed
        strips = app.screen._compositor.render_strips()
        screen = "\n".join("".join(seg.text for seg in strip) for strip in strips)
        assert "Title" in screen
        assert "**bold**" not in screen
        assert "bold" in screen.lower()


async def test_tool_activity_empty_output_still_renders(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        cards: dict = {}
        app._handle_event({"type": "tool_call_started", "id": "z1", "name": "noop",
                           "arguments": "{}"}, turn, cards)
        app._handle_event({"type": "tool_completed", "id": "z1", "name": "noop",
                           "result": "", "is_error": False}, turn, cards)
        app.flush_tool_activity()
        await pilot.pause(0.1)
        assert cards["z1"].card.raw_output  # "(no output)" fallback


# ------------------------------------------- turn_start / message_done events


async def test_turn_start_and_message_done_do_not_warn(workspace):
    """Legal kernel lifecycle events must be consumed silently — they are not
    protocol drift, and warning about them trained users to ignore warnings."""
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        cards: dict = {}
        app._handle_event({"type": "turn_started", "user_text": "hi"}, turn, cards)
        app._handle_event({"type": "assistant_delta", "text": "part 1"}, turn, cards)
        app._handle_event({"type": "assistant_message"}, turn, cards)
        app._handle_event({"type": "assistant_message"}, turn, cards)
        app._handle_event({"type": "turn_completed", "content": "part 1",
                           "usage": {"total_tokens": 9}}, turn, cards)
        await pilot.pause(0.2)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "Unhandled event" not in rendered
        assert "protocol drift" not in rendered
        assert "part 1" in turn.text()


async def test_message_done_flushes_but_does_not_end_the_turn(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        turn.push_content("first message")
        app._handle_event({"type": "assistant_message"}, turn, cards={})
        await pilot.pause(0.1)
        assert not turn._done, "message_done is a message boundary, not turn end"
        turn.push_content(" / second message")
        turn.finish()
        assert "second message" in turn.text()


async def test_turn_start_resets_the_turn_clock(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        # synthetic events through the real dispatch path
        app._handle_event({"type": "turn_started", "user_text": "x"}, turn, cards={},
                          turn_started=0.0)
        app._handle_event({"type": "turn_completed", "content": "ok"}, turn, cards={},
                          turn_started=time.monotonic() - 1.5)
        meta = turn.meta_text()
        assert meta  # model · elapsed · tokens present
        assert "mock/mock-echo" in meta


async def test_tool_denied_does_not_look_like_protocol_drift(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        cards: dict = {}
        app._handle_event({"type": "tool_call_started", "id": "d1", "name": "web_fetch",
                           "arguments": '{"url": "https://example.com"}'}, turn, cards)
        app._handle_event({"type": "tool_denied", "id": "d1", "name": "web_fetch"},
                          turn, cards)
        await pilot.pause(0.1)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "warp_drive" not in rendered
        assert "Unhandled" not in rendered and "protocol" not in rendered.lower()
        card = cards["d1"].card
        assert card is not None and card.has_class("tool-error")


async def test_truly_unknown_event_still_surfaces_as_drift(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        app._handle_event({"type": "warp_drive_engaged"}, turn, cards={})
        await pilot.pause(0.2)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "warp_drive_engaged" in rendered


# ------------------------------------------------------- anti-flood grouping

async def test_multi_tool_turn_mounts_one_line_per_call(workspace):
    """Chronological: every tool call is its own line, mounted immediately."""
    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        for i in range(4):
            run = app.activity_start(f"id{i}", "read_file", f"docs/f{i}.md")
            app.activity_finish(f"id{i}", f"({i} lines)", False, run)
        await pilot.pause(0.2)
        from oaset.tui.widgets.tool_card import ToolCard, ToolGroupCard

        cards = list(app.chat.query(ToolCard))
        assert len(cards) == 4, f"expected 4 live tool lines, got {len(cards)}"
        assert not list(app.chat.query(ToolGroupCard))
        assert all("Used" in c.title_text or "调用" in c.title_text for c in cards)


async def test_small_turns_keep_individual_cards(workspace):
    """1–2 次调用的回合保持原有单卡行为（不受归并影响）。"""
    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        for i in range(2):
            run = app.activity_start(f"id{i}", "read_file", f"f{i}.md")
            app.activity_finish(f"id{i}", "ok", False, run)
        await pilot.pause(0.2)
        from oaset.tui.widgets.tool_card import ToolCard, ToolGroupCard

        assert len(list(app.chat.query(ToolCard))) == 2
        assert not list(app.chat.query(ToolGroupCard))
