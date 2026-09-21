"""P0 stability regressions (docs/PLAN.zh-CN.md §2).

Every test here pins one of the eleven P0 fixes: the event loop must never
block on sync I/O, runaway tool loops must be bounded, and the small
correctness bugs found in the 2026-09-13 audit must stay fixed.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

from oaset.agent.loop import AgentLoop
from oaset.agent.messages import Conversation, ToolCallReq
from oaset.config import default_config
from oaset.tools.base import ToolContext
from oaset.tools.url_safety import check_url_async
from oaset.utils import kill_tree_sync, run_io

# ---------------------------------------------------------------- P0-3 caps


def test_iteration_and_tool_budget_defaults():
    cfg = default_config()
    assert cfg.max_iterations == 0  # default unlimited (user decision); >0 opts back in
    assert cfg.max_tool_calls == 0  # default unlimited (identical-call breaker stays)
    assert cfg.search.per_turn_search_budget == 0
    assert cfg.search.per_turn_fetch_budget == 0
    assert "web_fetch" not in cfg.disabled_tools


def _loop(max_tool_calls: int = 0) -> AgentLoop:
    return AgentLoop(
        provider=None,
        registry=None,
        conversation=Conversation(system_prompt="s"),
        max_tool_calls=max_tool_calls,
    )


def test_loop_guard_budget_blocks_after_limit():
    loop = _loop(max_tool_calls=2)
    call = ToolCallReq(id="1", name="read_file", arguments="{}")
    assert loop._loop_guard(call) is None
    assert loop._loop_guard(call) is None
    blocked = loop._loop_guard(call)
    assert blocked is not None and "budget" in blocked


def test_loop_guard_breaks_identical_repeat():
    loop = _loop()
    call = ToolCallReq(id="1", name="run_shell", arguments='{"command":"x"}')
    assert loop._loop_guard(call) is None  # 1st
    assert loop._loop_guard(call) is None  # 2nd identical
    third = loop._loop_guard(call)  # 3rd identical -> breaker
    assert third is not None and "identical" in third
    other = ToolCallReq(id="2", name="run_shell", arguments='{"command":"y"}')
    assert loop._loop_guard(other) is None  # different args resets the counter


# ------------------------------------------------------------------ P0-4 IO


async def test_run_io_single_worker_preserves_order():
    seen: list[int] = []

    async def push(i: int) -> None:
        await run_io(seen.append, i)

    await asyncio.gather(*(push(i) for i in range(100)))
    assert seen == list(range(100))


# ------------------------------------------------------------ P0-11 killing


def test_kill_tree_sync_kills_child_process():
    if sys.platform == "win32":
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    else:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                start_new_session=True)
    try:
        kill_tree_sync(proc.pid)
        assert proc.wait(timeout=10) != 0
    finally:
        if proc.poll() is None:
            proc.kill()


# ------------------------------------------------- P0-1 DNS off the loop


async def test_check_url_async_blocks_without_dns():
    # scheme and localhost checks short-circuit before any resolver call
    assert await check_url_async("ftp://example.com") is not None
    assert await check_url_async("http://localhost/x") is not None


async def test_check_url_async_enforces_dns_budget(monkeypatch):
    import oaset.tools.url_safety as us

    def slow_check(url, **kwargs):
        time.sleep(0.5)
        return None

    monkeypatch.setattr(us, "check_url", slow_check)
    start = time.monotonic()
    verdict = await us.check_url_async("http://example.com", timeout=0.05)
    assert verdict is not None and "DNS budget" in verdict
    assert time.monotonic() - start < 0.4  # the loop was not parked for 0.5s


# ------------------------------------------------------------ P0-6 ask_user


async def test_ask_user_timeout_returns_error(tmp_path, monkeypatch):
    import oaset.tools.ask_user as au

    async def never(question, choices):
        await asyncio.sleep(30)

    ctx = ToolContext(cwd=tmp_path, mode="default", output_limit=500)
    ctx.session_state["ask_user"] = never
    monkeypatch.setattr(au, "ASK_USER_TIMEOUT", 0.05)
    result = await au.AskUserQuestionTool().run(
        {"question": "继续吗?", "choices": ["a", "b"]}, ctx)
    assert result.is_error
    assert "超时" in result.output


# --------------------------------------------- P0-9 / P0-10 input & console


async def test_single_click_reaches_textarea_once(workspace, monkeypatch):
    """The duplicated super()._on_mouse_down() call (P0-9) must stay dead:
    exactly ONE TextArea mouse-down per physical click."""
    from textual.widgets import TextArea

    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        calls = {"n": 0}
        orig = TextArea._on_mouse_down

        async def counting(self, event):
            calls["n"] += 1
            await orig(self, event)

        monkeypatch.setattr(TextArea, "_on_mouse_down", counting)
        await pilot.click(app.input_area)
        await pilot.pause()
        assert calls["n"] == 1


def test_win_mouse_mode_includes_extended_flags():
    """QuickEdit-bit changes only take effect with ENABLE_EXTENDED_FLAGS set
    (MSDN); the fixed mode must carry it and must NOT re-enable QuickEdit."""
    from oaset.tui import _win_mouse_fix as fix

    assert fix.FIXED_INPUT_MODE & fix.ENABLE_EXTENDED_FLAGS
    assert not fix.FIXED_INPUT_MODE & fix.ENABLE_QUICK_EDIT_MODE
    assert fix.FIXED_INPUT_MODE & fix.ENABLE_VIRTUAL_TERMINAL_INPUT
    assert fix.FIXED_INPUT_MODE & fix.ENABLE_MOUSE_INPUT


async def test_io_barrier_times_out_when_queue_is_stuck():
    """Freeze-fix regression: a wedged IO op (AV lock / sync client) must make
    the barrier fail VISIBLY (IoQueueBlockedError) instead of hanging forever."""
    import threading
    import time as _time

    import pytest

    from oaset.utils import IoQueueBlockedError, io_barrier, run_io

    release = threading.Event()

    def stuck_write() -> None:
        release.wait(timeout=10)  # simulates a file locked by AV/sync client

    fut = run_io(stuck_write)  # occupies the single FIFO worker; do NOT await
    await asyncio.sleep(0.05)  # let it start
    started = _time.monotonic()
    with pytest.raises(IoQueueBlockedError):
        await io_barrier(timeout=0.2)
    assert _time.monotonic() - started < 5  # bounded, not forever
    release.set()
    await fut
    await io_barrier(timeout=5)  # queue drains once the op finishes


async def test_io_barrier_reports_stall_seconds():
    import threading

    import pytest

    from oaset.utils import IoQueueBlockedError, io_barrier, run_io

    release = threading.Event()

    def slow() -> None:
        release.wait(timeout=10)

    fut = run_io(slow)  # occupies the worker; do NOT await
    try:
        await asyncio.sleep(0.05)
        with pytest.raises(IoQueueBlockedError) as excinfo:
            await io_barrier(timeout=0.1)
        assert "running for" in str(excinfo.value)
    finally:
        release.set()
        await fut
        await io_barrier(timeout=5)
