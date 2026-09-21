"""Teardown race regressions (2026-09-14 real-machine crash).

Exiting (double Ctrl+C) while a turn was still finishing mounted notices and
the plan strip into an ALREADY-UNMOUNTED tree — Textual raised MountError and
the dying app showed a 30-frame traceback instead of exiting. Every dynamic
mount now checks attachment first.
"""

from __future__ import annotations

import asyncio

from oaset.config import default_config
from oaset.providers import MockProvider
from oaset.tui.app import OasetApp


class HangProvider(MockProvider):
    """Streams one chunk, then waits — the 'network retry' window."""

    async def stream(self, messages, tools):
        yield {"type": "text", "text": "waiting"}
        await asyncio.sleep(30)


async def test_exit_mid_turn_does_not_crash(workspace):
    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=HangProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        app.submit_text("hello")
        for _ in range(60):
            await pilot.pause(0.05)
            if app._worker_running():
                break
        assert app._worker_running(), "turn must be in flight when exiting"
        app.exit()  # double-Ctrl+C equivalent
        await pilot.pause(1.0)
    # reaching here without an exception IS the regression test: the worker's
    # teardown (interrupt notice, sidebar refresh, queue notices) must survive
    # an unmounted widget tree


async def test_interrupt_mid_retry_still_notifies_when_alive(workspace):
    """The same teardown guards must NOT swallow the normal interrupt notice
    while the app is alive: Esc during a hang still says 已被用户中断."""
    from oaset.i18n import set_language

    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=HangProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        set_language("zh")
        app.submit_text("hello")
        for _ in range(60):
            await pilot.pause(0.05)
            if app._worker_running():
                break
        await app.action_interrupt()
        for _ in range(40):
            await pilot.pause(0.05)
            if not app._worker_running():
                break
        strips = app.screen._compositor.render_strips()
        text = "\n".join("".join(seg.text for seg in s) for s in strips)
        assert "中断" in text
