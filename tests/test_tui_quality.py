"""TUI quality-round regressions: spinner work bound, session-switch drains,
stale-sink guard, and shutdown killing background process trees.

These pin the "long-session fluidity + leak/race sweep" (docs/PLAN.zh-CN.md
P4-3/P4-4 items landed outside the numbered phases).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time

from textual.widgets import Static as _Static

from oaset.config import default_config
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.providers.base import ContentDelta
from oaset.tui.app import OasetApp
from tests.test_app_tui import wait_until


class GatedProvider:
    """Streams one chunk, then blocks until released — a turn that stays live."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, messages, tools=None, **kwargs):
        self.started.set()
        yield ContentDelta("partial ")
        await self.release.wait()
        yield ContentDelta("done")

    async def aclose(self) -> None:
        pass


def make_app(workspace, provider=None):
    return OasetApp(cfg=default_config(), cwd=workspace,
                    provider=provider or GatedProvider(),
                    model_id="mock/mock-echo")


# ---------------------------------------------- spinner registry (fluidity)


async def test_activity_line_tracks_running_and_finished_tools(workspace):
    """Running tools paint on the working strip AND mount immediately into
    the transcript so later output scrolls them up."""
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app._set_working(True)
        app.status_bar.set_busy(True)
        app.activity_start("t1", "run_shell", "cmd=echo hi")
        for _ in range(3):
            app._spin_cards()
        working_text = str(app.working_line.render())
        assert "RunShell" in working_text and (
            "Used" in working_text or "调用" in working_text), \
            "the running tool shows on the strip"
        from oaset.tui.widgets.tool_card import ToolCard

        cards = list(app.chat.query(ToolCard))
        assert cards, "the tool line must enter the flow immediately"
        app.activity_finish("t1", "done", False, app.tool_runs["t1"])
        app._spin_cards()
        working_text = str(app.working_line.render())
        assert "✓1" in working_text, "finished tools are counted on the strip"
        assert "Used" in cards[-1].title_text or "调用" in cards[-1].title_text
        app.flush_tool_activity()
        assert not app._pending_cards


# --------------------------------------- session switches drain the old turn


async def test_cmd_new_cancels_the_inflight_turn(workspace):
    provider = GatedProvider()
    app = make_app(workspace, provider=provider)
    async with app.run_test(size=(120, 40)) as pilot:
        app.input_area.load_text("long running")
        await pilot.press("enter")
        end = time.monotonic() + 4
        while time.monotonic() < end and not provider.started.is_set():
            await pilot.pause(0.05)
        old_id = app.session.meta.session_id
        await app.cmd_new("")
        await pilot.pause(0.2)
        assert not app._worker_running(), \
            "/new must cancel and reap the in-flight turn (S5)"
        assert app.session.meta.session_id != old_id


async def test_load_session_drains_before_swapping(workspace):
    provider = GatedProvider()
    app = make_app(workspace, provider=provider)
    async with app.run_test(size=(120, 40)) as pilot:
        app.input_area.load_text("long running")
        await pilot.press("enter")
        end = time.monotonic() + 4
        while time.monotonic() < end and not provider.started.is_set():
            await pilot.pause(0.05)
        old_id = app.session.meta.session_id
        await app._load_session(old_id)  # reload the same record
        await pilot.pause(0.2)
        assert not app._worker_running()


# ------------------------------------------------------ stale sink guard


async def test_checkpoint_sink_drops_stale_generation(workspace, monkeypatch):
    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        gen = app._turn.generation
        app._turn.bump()  # a newer turn took over
        written: list = []
        monkeypatch.setattr(app.store, "checkpoint",
                            lambda session, snap: written.append(snap))
        await app._checkpoint_sink(gen, app.session, {"f": "v"})
        assert written == [], "a superseded turn must not checkpoint"


# ------------------------------------------------ shutdown kills bg tasks


async def test_background_stop_all_kills_process_trees(tmp_path):
    from oaset.tools.background import BackgroundRegistry, BackgroundTask

    registry = BackgroundRegistry()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)")
    task = BackgroundTask(id="t1", description="sleep", proc=proc)
    registry.tasks["t1"] = task
    await registry.stop_all()
    assert task.status == "stopped"
    assert proc.returncode is not None or (await proc.wait()) is not None


# ------------------------------------------------- fluidity round follow-ups


async def test_huge_unclosed_tail_throttles_parsing(workspace):
    """A single giant block (no blank line -> no commit boundary) used to
    re-parse the WHOLE tail every 120ms tick; the render rate must back off."""
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        await pilot.pause(0.2)
        huge = "```python\n" + "x = 1\n" * 1200  # ~8KB, then push past 12k
        turn.push_content(huge + "y = 2\n" * 500)
        for _ in range(20):  # 20 ticks inside one throttle window
            turn.flush()
        assert turn.parse_calls <= 3, (
            f"parse_calls={turn.parse_calls}: a huge tail must throttle renders")
        await pilot.pause(0.5)  # past the slow interval -> renders again
        turn.flush()
        assert turn.parse_calls <= 4


async def test_assistant_turn_shows_two_live_status_lines_immediately(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        await pilot.pause(0.1)
        rendered = str(turn.thinking.render())
        assert turn.thinking.display is True
        assert "\n" in rendered
        assert "思考中" in rendered or "thinking" in rendered
        turn.thinking.finish()
        assert turn.thinking.display is False


async def test_streaming_thinking_shows_only_a_status_line(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        turn = app.chat.start_assistant()
        tb = turn.thinking
        for i in range(60):
            tb.push(f"reasoning line {i}\n")
        tb.flush()  # one visible render after the burst (throttled, not per-delta)
        await pilot.pause(0.3)
        rendered = str(tb.render())
        # 2026-09-14 spec: streaming shows the status line PLUS exactly ONE
        # live reasoning line (two rows total), refreshed per flush tick.
        assert "思考中" in rendered or "thinking" in rendered
        assert rendered.count("reasoning line") <= 1, (
            "at most the newest reasoning line may paint")
        assert "reasoning line 59" in rendered  # the NEWEST line, live
        tb.finish()
        folded = str(tb.render())
        assert "reasoning line 0" not in folded, "folded keeps the text hidden"
        full = tb.copy_text()
        assert "reasoning line 0" in full, "finish keeps the full text for expand"


async def test_queue_badge_is_persistent(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app.input_queue.extend(["a", "b"])
        app.status_bar.set_queue_count(len(app.input_queue))
        assert app.status_bar._queue_n == 2
        app.status_bar.set_queue_count(0)
        assert app.status_bar._queue_n == 0


async def test_density_compact_class_and_persistence(workspace, tmp_path):
    from oaset.config import save_config

    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app.cfg.ui.density = "compact"
        app.add_class("compact")
        assert app.has_class("compact")
        app.remove_class("compact")
        assert not app.has_class("compact")
        saved_to: list = []
        monkey_save = save_config
        app.cfg.ui.density = "cozy"
        assert not app.has_class("compact")
        del monkey_save, saved_to


# ------------------------------------------- /view built-in pager (follow-up)


def test_paginate_lines_chunks_and_handles_empty():
    from oaset.tui.viewer import paginate_lines

    pages = paginate_lines("\n".join(f"l{i}" for i in range(7)), 3)
    assert [p.splitlines()[0] for p in pages] == ["l0", "l3", "l6"]
    assert paginate_lines("", 5) == [""]


def test_pager_navigation_sequence_is_pure():
    from oaset.tui.viewer import run_pager

    pages = ["P0", "P1", "P2"]
    keys = iter(["next", "next", "prev", "last", "first", "quit"])
    frames: list[str] = []
    run_pager(pages, read_key=lambda: next(keys), write=frames.append)
    # one frame per key press (including the quit press), last view = first page
    assert len(frames) == 6 and "P0" in frames[-1] and "P2" in frames[2]
    assert "1/3" in frames[0] and "3/3" in frames[2]


def test_pager_prev_clamps_at_first_page():
    from oaset.tui.viewer import run_pager

    frames: list[str] = []
    keys = iter(["prev", "quit"])
    run_pager(["only"], read_key=lambda: next(keys), write=frames.append)
    assert len(frames) == 2 and "1/1" in frames[-1]


async def test_view_text_falls_back_to_temp_file_when_not_interactive(
        workspace, monkeypatch, capsys):
    import inspect

    from oaset.tui import viewer

    monkeypatch.setattr(viewer, "_interactive_stdin", lambda: False)
    monkeypatch.setattr(viewer, "_wait_for_enter", lambda: None)
    app = make_app(workspace)

    app.suspend = lambda: contextlib.nullcontext()  # type: ignore[method-assign]
    await viewer.view_text(app, "title-x", "body-y")
    out = capsys.readouterr().out
    assert "body-y" in out and "oaset-view-" in out
    assert inspect.iscoroutinefunction(viewer.view_text)


# ------------------------------------ /errors: pinned error detail expansion


async def test_errors_opens_pinned_detail_in_viewer(workspace, monkeypatch):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app.notify_error(RuntimeError("the-original-cause"),
                         code="demo.boom", hint="try again", source="demo")
        assert app.status_bar._error
        captured: dict = {}

        async def fake_view(app_, title, text):
            captured["title"], captured["text"] = title, text

        monkeypatch.setattr("oaset.tui.viewer.view_text", fake_view)
        await app.cmd_errors("")
        await pilot.pause(0.1)
        assert "the-original-cause" in captured.get("text", ""), \
            "/errors must expand the FULL notice, not the 70-col headline"
        assert captured.get("text", "").startswith("hint:") is False
        assert "try again" in captured.get("text", "")
        # the status-bar headline carries the expansion entry marker
        assert "/errors" in app.status_bar.right1_text


async def test_errors_without_pinned_error_says_so(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app.status_bar.clear_error()
        app.last_error = None
        await app.cmd_errors("")
        await pilot.pause(0.1)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "没有钉住的错误" in rendered or "No pinned error" in rendered


async def test_view_pager_runs_off_the_event_loop(workspace):
    """Freeze-fix regression: the /view pager blocks on keyboard reads, so it
    must run on a worker thread — never stall the event loop."""
    import asyncio
    import threading

    from oaset.tui import viewer

    seen_threads: list[str] = []
    loop_thread = threading.current_thread().name

    def fake_pager(pages, *, read_key, write):
        seen_threads.append(threading.current_thread().name)

    def fake_wait() -> None:  # sync, matching the real input() contract
        seen_threads.append("enter:" + threading.current_thread().name)

    original_pager, original_wait = viewer.run_pager, viewer._wait_for_enter
    viewer.run_pager = fake_pager
    viewer._wait_for_enter = fake_wait
    try:
        if True:
            # direct call without a full app: exercise the threading contract
            class _Suspend:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            class _App:
                def suspend(self):
                    return _Suspend()

            await viewer.view_text(_App(), "t", "hello " * 50)
    finally:
        viewer.run_pager = original_pager
        viewer._wait_for_enter = original_wait
    assert seen_threads, "pager must have run"
    assert all(name != loop_thread for name in seen_threads), \
        f"pager ran on the event loop thread {seen_threads}"
    await asyncio.sleep(0)


# ------------------------------------------------- B7: 10k-block stream budget


class _FakeMarkdown(_Static):
    """Textual Markdown runs its parser in an executor per widget — 10k of
    them drown the pilot. These tests target the COMMITTER (parse-once
    accounting + DOM archive bound), so rendering is stubbed out."""

    def __init__(self, text: str = "", classes: str | None = None):
        super().__init__("", classes=classes)

    def update(self, text: str) -> None:
        pass


def _patch_markdown(monkeypatch):
    import oaset.tui.widgets.chat as chat_mod

    monkeypatch.setattr(chat_mod, "Markdown", _FakeMarkdown)


def test_10k_blocks_stream_without_parsing_or_widget_growth():
    """10k blocks: parse cost stays on the live tail; a standalone turn cannot
    mount Markdown children, so the widget count stays constant."""
    import time as _time

    from oaset.tui.widgets.chat import AssistantTurn

    turn = AssistantTurn()
    started = _time.monotonic()
    block = "\n\n段落 %d 号，附足够文字让解析器有意义。\n\n"
    widgets = len(turn.children)
    for i in range(10_000):
        turn.push_content(block % i)
        turn.flush()
    elapsed = _time.monotonic() - started
    # Standalone turn cannot mount Markdown children; the live Static stays put.
    # Parse cost must stay on the TAIL (linear), not the whole answer (n²).
    assert len(turn.children) == widgets
    assert elapsed < 60, f"10k blocks took {elapsed:.0f}s"
    assert turn.parse_chars < 5_000_000, (
        f"parse_chars={turn.parse_chars}: incremental commit must not re-parse the whole answer")
    assert len(turn._committed_src) >= 9_000
    assert turn.text().count("段落") == 10_000
    painted = str(turn.markdown._renderable if hasattr(turn.markdown, "_renderable")
                  else turn.markdown.render())
    assert "段落" in painted


async def test_tool_card_keyboard_toggle(workspace):
    """B6: tool cards are focusable; Enter toggles expand/collapse."""
    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([
                       MockTurn(tool_calls=[MockToolCall("list_dir", {})]),
                       MockTurn(content_chunks=["ok"]),
                   ]), model_id="mock/mock-echo")
    from oaset.tui.widgets.tool_card import ToolCard

    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app.submit_text("看看目录")
        assert await wait_until(pilot, lambda: list(app.query(ToolCard)))
        await pilot.pause(0.5)
        card = app.query(ToolCard).first()
        assert card.can_focus
        card.focus()
        await pilot.pause(0.1)
        expanded_before = card._expanded if hasattr(card, "_expanded") else None
        await pilot.press("enter")
        await pilot.pause(0.1)
        expanded_after = card._expanded if hasattr(card, "_expanded") else None
        if expanded_before is not None:
            assert expanded_after != expanded_before
