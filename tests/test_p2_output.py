"""P2 output-system regressions (docs/PLAN.zh-CN.md §4).

Pins the incremental markdown commitment (only the unfinished tail is ever
re-parsed), the reasoning throttle, chronological segmentation around tool
calls (post-tool text renders BELOW the tool card), display-width truncation,
recoverable truncation spill, the archive bound, and the /view payload.
"""

from __future__ import annotations

import time

from rich.cells import cell_len

from oaset.config import default_config
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.tui.app import OasetApp
from oaset.tui.widgets.chat import AssistantTurn, ThinkingBlock, split_committable
from oaset.tui.widgets.tool_card import ToolCard, peek_text
from oaset.utils import one_line


def make_app(workspace, script=None):
    provider = MockProvider(script or [])
    return OasetApp(cfg=default_config(), cwd=workspace, provider=provider,
                    model_id="mock/mock-echo")


# ------------------------------------------------- P2-1 incremental commit


def test_split_committable_commits_all_but_last_block():
    text = "# Title\n\ncolumn | col2\n--- | ---\na | b\n\nFinal para"
    head, tail = split_committable(text)
    assert head.startswith("# Title") and "column" in head
    assert "Final para" in tail


def test_split_committable_keeps_unclosed_fence_in_tail():
    head, tail = split_committable("intro\n\n```python\nprint(1)\n")
    assert "intro" in head and "```python" in tail


def test_split_committable_single_block_stays_whole():
    head, tail = split_committable("just one paragraph")
    assert head == "" and tail == "just one paragraph"


def test_split_committable_loose_list_never_splits():
    text = "para\n\n1. one\n\n2. two\n\nmore"
    head, tail = split_committable(text)
    assert "1. one" in head and "2. two" in head, "a loose list is ONE block"
    assert tail == "more"


async def test_streaming_commits_finished_blocks_and_keeps_the_tail_live(workspace):
    """Finished markdown blocks mount once; only the live tail is repainted."""
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        await pilot.pause(0.2)
        body = "\n\n".join(f"block {i} with some content" for i in range(12))
        for i in range(0, len(body), 2):
            turn.push_content(body[i:i + 2])
            turn.flush()
        await pilot.pause(0.2)
        assert turn.text() == body
        assert turn.parse_calls >= 1
        assert turn._committed_src, "closed blocks must be committed, not redrawn"
        assert "block 0" in "\n".join(turn._committed_src)
        painted = str(turn.live.render())
        assert "block 11" in painted and not painted.startswith("●")


# ------------------------------------------------------------ P2-2 throttle


def test_thinking_stream_is_throttled():
    tb = ThinkingBlock()
    for _ in range(200):
        tb.push("x")  # a fast burst lands inside one throttle window
    assert tb.render_count <= 3, "per-token re-rendering must stay batched"


# --------------------------------- P2 ordering: tool calls segment the answer


async def test_post_tool_text_renders_below_the_tool_card(workspace):
    script = [
        MockTurn(content_chunks=["before "],
                 tool_calls=[MockToolCall("read_file", {"path": "a.txt"})]),
        MockTurn(content_chunks=["after the tool"]),
    ]
    (workspace / "a.txt").write_text("data", encoding="utf-8")
    app = make_app(workspace, script)
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("go")
        await pilot.press("enter")
        end = time.monotonic() + 5
        # Activity mode: text streams first; the collapsed card lands at the
        # chronological anchor when the turn finishes — wait for BOTH.
        while time.monotonic() < end and not (
                "after the tool" in chat_text(app)
                and list(app.chat.query(ToolCard))):
            await pilot.pause(0.05)
        blocks = list(app.chat.children)
        kinds = [type(w).__name__ for w in blocks]
        assert "ToolCard" in kinds
        card_idx = kinds.index("ToolCard")
        above = blocks[card_idx - 1]
        below = [w for w in blocks[card_idx + 1:]
                 if isinstance(w, AssistantTurn)]
        assert "before" in above.text(), "narration before the tool stays above it"
        assert below and "after the tool" in below[-1].text(), \
            "post-tool narration must open a NEW segment BELOW the card"


def chat_text(app) -> str:
    return "\n".join(w.text() for w in app.chat.children
                     if isinstance(w, AssistantTurn))


# --------------------------------------------- P2-3 width-aware truncation


def test_one_line_cuts_by_display_cells():
    s = one_line("中" * 50, 20)
    assert cell_len(s) <= 20 and s.endswith("…"), "CJK counts as 2 cells"


def test_peek_text_is_one_line_within_width():
    out = peek_text("x" * 200, "  (+9 lines · ctrl+o)", 80)
    assert "\n" not in out and cell_len(out) <= 80
    cjk = peek_text("中文" * 100, "", 80)
    assert cell_len(cjk) <= 80 and "\n" not in cjk


async def test_truncated_tool_output_spills_full_text(workspace):
    from oaset.tools import AutoGate, ToolRegistry
    from oaset.tools.base import ToolContext

    big = workspace / "big.txt"
    big.write_text("\n".join(f"line {i}" for i in range(2000)), encoding="utf-8")
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=500)
    registry.bind(ctx)
    ctx.session_state["tool_output_dir"] = str(workspace / "spill")
    result = await registry.dispatch("read_file", '{"path": "big.txt"}')
    assert not result.is_error
    assert "chars truncated" in result.output or "完整输出已保存" in result.output
    assert "完整输出已保存" in result.output, "the marker must name the spill file"
    spills = list((workspace / "spill").glob("*.txt"))
    assert spills, "the full text must be recoverable from the spill file"
    assert len(spills[0].read_text(encoding="utf-8")) > len(result.output)


# ------------------------------------------------ P2-5 archive bound


async def test_chatlog_archives_old_blocks_and_reloads_at_top(workspace, monkeypatch):
    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app.chat.max_mounted_blocks = 25
        app.chat.archive_page = 10
        for i in range(40):
            app.chat.add_notice(f"notice {i}")
        from tests.test_app_tui import wait_until

        assert await wait_until(pilot, lambda: len(app.chat._archived) > 0)
        assert await wait_until(
            pilot, lambda: len(app.chat._live_blocks()) <= app.chat.max_mounted_blocks + 1)
        before = len(app.chat._archived)
        await app.chat._load_archived_page()
        await pilot.pause(0.1)
        assert len(app.chat._archived) < before, "an archive page reloads at the top"


# ------------------------------------------------------------ P2-4 /view


def test_render_view_contains_payload_and_hint():
    from oaset.tui.viewer import render_view

    out = render_view("assistant answer", "hello world")
    # the header no longer promises "Enter returns" (Enter pages or returns
    # depending on the page count — the footer owns the key hints now)
    assert "hello world" in out
    assert "拖选后复制" in out or "drag to select" in out
    assert "按 Enter 返回" not in out and "Press Enter to return" not in out


async def test_view_payload_picks_the_last_answer(workspace):
    from oaset.agent.messages import Message

    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app.conversation.append(Message(role="assistant", content="the answer"))
        _title, text = app._view_payload("answer")
        assert text == "the answer"
        _t2, empty = app._view_payload("tools")
        assert empty == ""


async def test_activity_stream_paints_the_running_card_peek(workspace):
    """Shell chunks used to sit in ToolRun.result only; the card peek must move."""
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        run = app.activity_start("s1", "run_shell", "pytest -q")
        app.activity_stream("collecting ...\n")
        app.activity_stream("FAILED tests/test_x.py::test_a\n")
        await pilot.pause(0.1)
        card = run.card
        assert card is not None
        assert "FAILED" in card.raw_output
        painted = str(card._peek.render())
        assert "FAILED" in painted
        assert card._peek.display is True
