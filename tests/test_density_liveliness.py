"""Density & liveliness regressions (2026-09-14 screenshot pass).

The screenshot complaint: one wall of `✓ Used …` lines, a 30-line notice
dump, a borderless input and a busy bar that read as a hang.
"""

from __future__ import annotations

from oaset.agent.verify import implies_system, nudge_text
from oaset.config import default_config
from oaset.providers import MockProvider, MockTurn
from oaset.tui.app import OasetApp
from oaset.tui.widgets.chat import FoldChip, NoticeCard
from oaset.tui.widgets.tool_card import ToolCard


def make_app(workspace, turns=None) -> OasetApp:
    cfg = default_config()
    cfg.ui_language = "en"
    return OasetApp(cfg=cfg, cwd=workspace,
                    provider=MockProvider(turns or [MockTurn(content_chunks=["ok"])]),
                    model_id="mock/mock-echo")


async def test_consecutive_tool_calls_fold_behind_one_chip(workspace):
    """5+ consecutive finished tool cards collapse to chip + 2 newest cards;
    the cards stay in the DOM so copy/transcript are unaffected."""
    app = make_app(workspace)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        cards = []
        for i in range(7):
            run = app.activity_start(f"id{i}", "read_file", f"path=f{i}.py")
            app.activity_finish(f"id{i}", f"output {i}", False, run)
            cards.append(run.card)
        await pilot.pause(0.2)
        chips = list(app.chat.query(FoldChip))
        assert len(chips) == 1
        hidden = [c for c in cards if not c.display]
        assert len(hidden) == 5, "7 calls fold to chip + 2 visible"
        assert "5" in str(chips[0].content), "chip label carries the count"
        # copy still sees every card (hidden ones included)
        copied = app.chat.tool_outputs_text()
        assert all(f"output {i}" in copied for i in range(7))
        # expanding the chip restores visibility
        await chips[0].on_click()
        await pilot.pause(0.2)
        assert all(c.display for c in app.chat.query(ToolCard))


async def test_short_tool_runs_do_not_fold(workspace):
    """4 or fewer consecutive calls stay fully visible (threshold=5)."""
    app = make_app(workspace)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        for i in range(4):
            run = app.activity_start(f"id{i}", "read_file", f"p{i}")
            app.activity_finish(f"id{i}", "x", False, run)
        await pilot.pause(0.2)
        assert not list(app.chat.query(FoldChip))
        assert len([c for c in app.chat.query(ToolCard) if c.display]) == 4


def test_notice_display_clamped_copy_full():
    card = NoticeCard("\n".join(f"line {i}" for i in range(1, 31)), "info")
    shown = card.renderable if hasattr(card, "renderable") else str(card.content)
    shown_text = str(shown)
    assert "line 12" in shown_text
    assert "line 30" not in shown_text, "display must stop at the clamp line"
    assert "复制可得全文" in shown_text or "copy keeps" in shown_text
    # copy keeps EVERYTHING
    assert card.copy_text().count("line") == 30


def test_implies_system_and_stack_checklist_nudge():
    assert implies_system("帮我搭一个带登录的网站")
    assert implies_system("build a webapp with a dashboard")
    assert not implies_system("解释这段正则")
    plain = nudge_text(["a.py"])
    system = nudge_text(["a.py"], system=True)
    assert "Stack checklist" in system
    assert "Stack checklist" not in plain


async def test_stall_signal_wired(workspace):
    """The bar reports seconds-without-progress while busy; the app feeds it."""
    app = make_app(workspace)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        assert app.status_bar.stalled_for is not None, "app must wire the callback"
        # 20s of silence reads as a stall in the bar's busy text
        app.status_bar.set_busy(True)
        app._last_activity_at -= 20
        app.status_bar._refresh()
        assert "no output" in app.status_bar.left_text or "无输出" in app.status_bar.left_text
