"""The gradient floor: one hairline between the conversation and the
status bar, carrying the brand ramp across the whole terminal width.

Same rules as the welcome page: colour is gated on truecolor support
(modern conhost included), the ramp flips for light themes, and the line
rebuilds on resize / theme switch because its spans bake at build time.
Height is a fixed 1 row, so it cannot fall into the auto-height trap.
"""

from __future__ import annotations

from textual.widgets import Static


class BrandDivider(Static):
    DEFAULT_CSS = "BrandDivider { height: 1; width: 1fr; margin: 0; }"

    def __init__(self) -> None:
        super().__init__("", markup=False)

    def _build(self) -> None:
        from oaset.tui import brand

        colored = brand.truecolor_supported()
        theme = None
        try:
            theme = self.app.current_theme
        except Exception:
            pass
        dark = getattr(theme, "dark", True)
        ramp = brand.GRADIENT if dark else brand.GRADIENT_LIGHT
        width = max(int(self.size.width) or 80, 1)
        self.update(brand.rule(width, colored, ramp))

    def on_mount(self) -> None:
        self._build()

    def on_resize(self) -> None:
        self._build()

    def refresh_brand(self) -> None:
        """Theme switch hook — the ramp flips between dark and light."""
        self._build()
