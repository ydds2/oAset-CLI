"""Brand assets: the wordmark must be geometrically honest (every row the
same width or the block letters shear apart) and the gradient ramp must
actually tint — a flat ramp reads as a bug, not a brand."""

from __future__ import annotations

import sys

from oaset.tui import brand


def test_wordmark_rows_are_rectangular():
    widths = {len(line) for line in brand.WORDMARK}
    assert len(widths) == 1, f"sheared rows: {widths}"
    assert brand.WORDMARK_HEIGHT == 6
    assert brand.WORDMARK_WIDTH >= 40  # the block art, not a stub


def test_wordmark_is_tinted_by_the_ramp():
    text = brand.wordmark()
    assert text.plain == "\n".join(brand.WORDMARK)
    colors = {span.style for span in text.spans}
    assert len(colors) > 20, "a gradient needs many stops, not a flat colour"


def test_rule_width_and_ramp_endpoints():
    rule = brand.rule(24)
    assert rule.plain == "─" * 24
    assert len(rule.spans) == 24
    assert brand.gradient_hex(0.0) == "#3b5f9e"
    assert brand.gradient_hex(1.0) == "#a8e6ff"
    assert brand.gradient_hex(-1) == brand.gradient_hex(0.0)   # clamped
    assert brand.gradient_hex(2) == brand.gradient_hex(1.0)    # clamped


def test_legacy_wordmark_is_solid_not_spanned():
    plain = brand.wordmark(colored=False)
    assert not plain.spans  # conhost gets one colour, no per-character cost


def test_light_ramp_stays_readable_on_pale_backgrounds():
    """The dark ramp's ice-cyan tail (#a8e6ff) vanishes on white; the light
    ramp must end dark enough to keep contrast against a pale console."""
    assert brand.gradient_hex(1.0) == "#a8e6ff"
    assert brand.gradient_hex(1.0, brand.GRADIENT_LIGHT) == "#5b9bd5"
    dark_tints = {span.style for span in brand.wordmark().spans}
    light_tints = {span.style for span in brand.wordmark(ramp=brand.GRADIENT_LIGHT).spans}
    assert dark_tints and light_tints and dark_tints != light_tints


def test_truecolor_detection_covers_modern_conhost(monkeypatch):
    """cmd.exe on Windows 10 19041+/11 renders VT truecolour — the gradient
    must not be gated on Windows Terminal alone."""
    monkeypatch.setenv("WT_SESSION", "1")
    assert brand.truecolor_supported()
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)

    monkeypatch.setattr(sys, "platform", "linux")
    assert brand.truecolor_supported()

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "getwindowsversion",
                        lambda: type("Win", (), {"build": 17763})())
    assert not brand.truecolor_supported()      # pre-19041 conhost: solid
    monkeypatch.setattr(sys, "getwindowsversion",
                        lambda: type("Win", (), {"build": 26200})())
    assert brand.truecolor_supported()


async def test_welcome_degrades_below_wordmark_width(workspace):
    """A 38-column window must drop the art (a sheared wordmark is worse
    than none) but keep every session fact."""
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp
    from oaset.tui.widgets.chat import WelcomePanel

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(38, 24)) as pilot:
        await pilot.pause(0.3)
        panel = app.chat.query(WelcomePanel).first()
        rendered = str(panel.render())
        assert "█" not in rendered
        assert str(workspace) in rendered


async def test_welcome_drops_the_art_without_truecolor(workspace, monkeypatch):
    """A pre-19041 raster console gets NO block letters at all — box-drawing
    garbles there exactly like the legacy spinner glyphs, colour or not."""
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp
    from oaset.tui.widgets.chat import WelcomePanel

    monkeypatch.setattr(brand, "truecolor_supported", lambda: False)
    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause(0.3)
        rendered = str(app.chat.query(WelcomePanel).first().render())
        assert "█" not in rendered and "╗" not in rendered
        assert str(workspace) in rendered
