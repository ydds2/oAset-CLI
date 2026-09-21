"""oMotion — oAset's OWN motion language (nothing borrowed).

Every moving glyph is the oAset "o": the busy DIAL fills quadrant by
quadrant then drains, thinking is a RIPPLE, todos are the dial's three
moments. These tests pin the design AND that no borrowed convention
(braille rotation, moon phases) or stale set survives anywhere.
"""

from __future__ import annotations

from pathlib import Path

from oaset.tui.glyphs import (
    RIPPLE,
    SPINNER_DIAL,
    SPINNER_DOT,
    SPINNER_INTERVAL,
    STATIC,
    TODO_GLYPHS,
    ripple_frame,
    spinner_frame,
    spinner_frames,
)

ROOT = Path(__file__).resolve().parents[1] / "src"
BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
MOON = "◐◓◑◒"


# ------------------------------------------------------------------- the design

def test_dial_is_a_fill_then_drain_breath():
    assert SPINNER_DIAL == "○◔◑◕●◕◑◔"
    assert len(SPINNER_DIAL) == 8
    assert len(set(SPINNER_DIAL)) == 5  # symmetric fill/drain
    # the fill actually grows: ○ empty → ● full at the midpoint
    assert SPINNER_DIAL[0] == "○" and SPINNER_DIAL[4] == "●"
    assert SPINNER_INTERVAL <= 0.15, "sub-150ms reads as motion, not stepping"


def test_ripple_radiates_out_and_back():
    assert RIPPLE[0] == "·" and RIPPLE[len(RIPPLE) // 2] == "○"
    # out: ·∘◌○, back: ◌∘ — the point itself never repeats on the way back
    assert RIPPLE == "·∘◌○" + "◌∘", "radiates out, then recedes"
    # phase-locked: same wall second → same frame everywhere
    assert ripple_frame(1.0) == ripple_frame(1.0)
    assert ripple_frame(0.0) != ripple_frame(0.3)


def test_todo_states_are_the_dials_three_moments():
    # the todo set is drawn FROM the dial: not-started / in-motion / done
    assert TODO_GLYPHS["pending"] == SPINNER_DIAL[0]
    assert TODO_GLYPHS["in_progress"] == SPINNER_DIAL[1]
    assert TODO_GLYPHS["completed"] == SPINNER_DIAL[4]
    assert STATIC["busy"] == "◎" and STATIC["tool_running"] == "◍"


def test_legacy_falls_back_to_glyphs_every_font_has():
    class _App:
        def __init__(self, legacy=False):
            self._legacy = legacy

        def has_class(self, name):
            return self._legacy and name == "legacy"

    assert spinner_frames(_App()) == SPINNER_DIAL
    assert spinner_frames(_App(legacy=True)) == SPINNER_DOT
    assert set(SPINNER_DOT) <= {"·", "○", "●"}
    assert spinner_frame(_App(), 8) == SPINNER_DIAL[0]  # wraps
    assert spinner_frame(None, 3) in SPINNER_DIAL  # app-less is safe


# --------------------------------------------------------------- originality

def test_no_borrowed_or_stale_sets_anywhere():
    """Neither our retired set nor the borrowed conventions (braille
    rotation, moon phases) may appear anywhere in the product code."""
    offenders = []
    for path in (ROOT / "oaset" / "tui").rglob("*.py"):
        if path.name == "glyphs.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, needle in (("old-set", "⁝⋮⋰⋱"),
                              ("braille", BRAILLE),
                              ("moon-mid", "◐◓◑◒"),
                              ("moon-glyph-set", "◐") ):
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}:{label}")
    assert not offenders, offenders


def test_consumers_reference_the_registry():
    status = (ROOT / "oaset" / "tui" / "widgets" / "status_bar.py").read_text(encoding="utf-8")
    assert "SPINNER_DIAL" in status and "SPINNER_INTERVAL" in status
    card = (ROOT / "oaset" / "tui" / "widgets" / "tool_card.py").read_text(encoding="utf-8")
    assert "spinner_frame" in card
    chat = (ROOT / "oaset" / "tui" / "widgets" / "chat.py").read_text(encoding="utf-8")
    assert "ripple_frame" in chat, "thinking uses our ripple"
