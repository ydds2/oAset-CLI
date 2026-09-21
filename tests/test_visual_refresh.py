"""Live refresh of the welcome page: language and theme both change it
after it painted, and its spans are baked at build time — so both switches
must remount it (the login path already did; 2026-09 audit found these two
did not, leaving a Chinese page under /language en and an ice-cyan wordmark
washed out under oaset-light)."""

from __future__ import annotations

from oaset.config import default_config
from oaset.providers import MockProvider
from oaset.tui.app import OasetApp
from oaset.tui.widgets.chat import WelcomePanel


async def test_language_switch_re_renders_the_welcome_page(workspace):
    cfg = default_config()
    cfg.ui_language = "zh"
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        before = app.chat.query(WelcomePanel).first()
        assert "本地优先" in before.raw_text

        app._apply_language("en")
        await pilot.pause(0.3)

        panels = list(app.chat.query(WelcomePanel))
        assert len(panels) == 1, "exactly one welcome page after the swap"
        assert panels[0] is not before, "the page must be remounted, not stale"
        assert "local-first" in panels[0].raw_text


async def test_theme_switch_re_renders_the_welcome_ramp(workspace):
    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app._apply_theme("oaset-light")
        await pilot.pause(0.3)

        panel = app.chat.query(WelcomePanel).first()
        tints = {str(span.style) for span in panel.render().spans}
        # the light ramp starts navy rgb(23,50,94) — the dark ramp never goes
        # that dark at its start (#3b5f9e = rgb(59,95,158))
        assert any(tint.startswith("rgb(23,") for tint in tints), \
            f"light-ramp navy missing from {sorted(tints)[:5]}..."
        assert app.theme == "oaset-light"
