"""Fast-boot regressions (2026-09-14): paint first, warm the runtime after.

A real `oaset` start (no injected provider) used to pay the openai import
(~1.6s) inside OasetApp.__init__, in front of the first frame. The host now
builds in a boot worker; the welcome paints immediately and submits made
during the window are held and auto-sent.
"""

from __future__ import annotations

import asyncio

from oaset.config import default_config
from oaset.tui.app import OasetApp
from oaset.tui.widgets.chat import WelcomePanel


def screen_text(app) -> str:
    strips = app.screen._compositor.render_strips()
    return "\n".join("".join(seg.text for seg in strip) for strip in strips)


async def test_fast_boot_paints_then_hosts_and_holds_sends(workspace):
    cfg = default_config()
    cfg.default_model = "mock/mock-echo"
    cfg.default_provider = "mock"
    app = OasetApp(cfg=cfg, cwd=workspace, model_id="mock/mock-echo")  # provider=None → fast path
    # make the boot window deterministic: hold the worker on an event
    gate = asyncio.Event()
    real_boot = OasetApp._boot_host_async

    async def gated_boot(self):
        await gate.wait()
        await real_boot(self)

    app._boot_host_async = gated_boot.__get__(app)  # type: ignore[method-assign]
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.1)
        # first frame: welcome painted, runtime not yet built
        assert app.host is None
        screen = screen_text(app)
        # the welcome PANEL must carry the branding; screen visibility also
        # depends on how much content sits below it (a submitted message
        # scrolls the chat), which varies with the runner's real console
        from oaset.tui.widgets.chat import WelcomePanel

        panel_text = str(app.chat.query(WelcomePanel).first().render())
        assert "oAset" in panel_text and "mock/mock-echo" in panel_text
        assert "oAset" in screen or "mock/mock-echo" in screen
        session_id = app.session.meta.session_id

        # a slash command during the window is refused with a hint, not a crash
        app.submit_text("/help")
        await pilot.pause(0.1)
        assert "初始化" in screen_text(app) or "warming" in screen_text(app).lower()

        # a message during the window is held, not lost — and a second one
        # during the same window is queued too (overwrite used to drop it)
        app.submit_text("held message")
        await pilot.pause(0.1)
        assert app._boot_pending == ["held message"]
        app.submit_text("second held")
        await pilot.pause(0.1)
        assert app._boot_pending == ["held message", "second held"]

        # boot worker completes: host/kernel wired, SAME session on screen
        gate.set()
        for _ in range(200):
            await pilot.pause(0.05)
            if app.host is not None:
                break
        await pilot.pause(0.3)
        assert app.host is not None
        assert app.registry is not None and app._kernel is not None
        assert app.session.meta.session_id == session_id, "host must adopt the pre-created session"
        assert len(list(app.chat.query(WelcomePanel))) == 1, "no duplicate welcome after boot"

        # the held messages auto-sent once, in order — the second runs after
        # the first turn ends, so wait (bounded) rather than pause-and-hope
        assert app._boot_pending == []
        for _ in range(200):
            await pilot.pause(0.05)
            msgs = [str(m.content) for m in app.conversation.messages if m.role == "user"]
            if any("second held" in m for m in msgs):
                break
        sent = [m for m in app.conversation.messages
                if m.role == "user" and "held message" in str(m.content)]
        assert len(sent) == 1
        sent2 = [m for m in app.conversation.messages
                 if m.role == "user" and "second held" in str(m.content)]
        assert len(sent2) == 1


async def test_injected_provider_stays_eager(workspace):
    """Tests and --mock keep the synchronous path: host exists before mount."""
    from oaset.providers import MockProvider

    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    assert app.host is not None and app.provider is not None
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.1)
        assert app.host is not None


def test_first_frame_path_stays_lazy():
    """The welcome must paint WITHOUT paying for tools/web/httpx/agent/openai:
    these were deferred to the boot worker once — a module-level import
    anywhere on the paint path silently brings them back."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import oaset.tui.app  # the paint path\n"
        "banned = [m for m in ('oaset.tools.web', 'oaset.tools.subagent',"
        " 'oaset.agent', 'httpx', 'openai')\n"
        "          if m in sys.modules or any(k.startswith(m + '.')"
        " for k in sys.modules)]\n"
        "assert not banned, f'first-frame path imported: {banned}'\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]


def test_boot_held_messages_render_once():
    """The hold path renders the bubble; the drain re-submitted WITHOUT
    from_queue, so every message held during warm-up showed TWICE for the
    rest of the session."""
    import tempfile
    from pathlib import Path

    class GatedProvider(__import__("oaset.providers", fromlist=["MockProvider"]).MockProvider):
        def __init__(self):
            super().__init__([__import__("oaset.providers", fromlist=["MockTurn"]).MockTurn(
                content_chunks=["done"])])
            import asyncio as _a
            self.release = _a.Event()

        async def stream(self, messages, tools):
            await self.release.wait()
            async for event in super().stream(messages, tools):
                yield event

    provider = GatedProvider()
    app = OasetApp(cfg=default_config(), cwd=Path(tempfile.mkdtemp()),
                   provider=provider, model_id="mock/mock-echo")
    async def run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.submit_text("held once")
            await pilot.pause(0.1)
            from oaset.tui.widgets.chat import UserMessage
            for _ in range(80):
                await pilot.pause(0.1)
                bubbles = [w for w in app.chat.query(UserMessage)]
                [getattr(w, "_text_src", "") or "" for w in bubbles]
                rendered = " ".join(str(w.render()) for w in bubbles)
                if app.host is not None and "held once" in rendered:
                    break
            provider.release.set()
            await pilot.pause(0.3)
            count = sum("held once" in str(w.render())
                        for w in app.chat.query(UserMessage))
            assert count == 1, f"held message rendered {count} times"
    import asyncio
    asyncio.run(run())


async def test_welcome_never_scrolls_its_branding_off(workspace, monkeypatch):
    """CI repro: with truecolor active (wordmark admitted) the panel used
    to exceed the chat viewport and scroll "oAset" off the top on a 30-row
    screen. The budget now clamps to screen height minus chrome."""
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn
    from oaset.tui import brand as brand_mod
    from oaset.tui.app import OasetApp

    monkeypatch.setattr(brand_mod, "truecolor_supported", lambda: True)
    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        panel = app.chat.query(WelcomePanel).first()
        # the invariant is "fits the screen it is actually in": the CI
        # runner's console is larger than run_test's requested 30 rows, so
        # compare against the REAL screen height, not a hardcoded one
        real_h = int(app.screen.size.height)
        assert panel.region.height <= real_h - 4, (
            f"welcome panel is {panel.region.height} rows on a {real_h}-row "
            "screen: it will scroll its own branding off")
        assert "oAset" in str(panel.render()), "branding must render"


async def test_welcome_build_does_not_loop(workspace):
    """The set-height→resize→rebuild chain must not oscillate: an
    unconditional height write with disagreeing size measurements was the
    full-gate 'hang' (rebuild loop burning CPU/memory). Count builds."""
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn
    from oaset.tui.app import OasetApp

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        panel = app.chat.query(WelcomePanel).first()
        builds = {"n": 0}
        real_build = panel._build

        def counting_build() -> None:
            builds["n"] += 1
            if builds["n"] > 10:
                raise AssertionError("welcome rebuild loop")
            real_build()

        panel._build = counting_build  # type: ignore[method-assign]
        for _ in range(40):
            await pilot.pause(0.05)
        assert builds["n"] <= 2, f"{builds['n']} rebuilds in 2s: loop"
