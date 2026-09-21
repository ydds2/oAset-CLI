"""oMotion — oAset's own motion language: every moving glyph is the "o".

Borrowed conventions (braille rotation, moon phases) were replaced by one
original system with a single idea behind it: the oAset "o" IS the state.

  DIAL  ○◔◑◕●◕◑◔  the busy "o" fills quadrant by quadrant, then drains —
       a breathing dial. Work fills the circle; the full ● is completion.
  RIPPLE ·∘◌○◌∘  thinking radiates outward from a point — thought as ripple.
  TODO   ○ ◔ ●    the dial's three moments: not started / in motion / done.

Cadence is part of the design: 120ms/frame gives the 8-frame dial a ~0.96s
breath; the ripple runs slow (250ms) because thinking is calm. Static
reduce-motion marks are the same "o" at rest (◎ ◍). Terminal-safe: every
glyph is a Geometric Shapes basic; the legacy (non-Windows-Terminal) class
falls back to a plain dot-pulse (·○●○) that conhost's raster font cannot
mangle.

Pass/fail marks stay ✔/✗ deliberately: those are semantic traffic lights,
not identity — clarity outranks novelty for error reporting.
"""

from __future__ import annotations

# oMotion frames ---------------------------------------------------------------

# busy: the o fills and drains, quadrant by quadrant (8 frames ≈ 0.96s)
SPINNER_DIAL = "○◔◑◕●◕◑◔"
# thinking: a ripple from a point outward and back (slow, 6 frames ≈ 1.5s)
RIPPLE = "·∘◌○◌∘"
# legacy fallback: only glyphs every raster font has
SPINNER_DOT = "·○●○"

SPINNER_INTERVAL = 0.12  # dial cadence — sub-150ms reads as motion, not steps
RIPPLE_INTERVAL = 0.25

# the "o" at rest (reduce_motion) + semantic traffic lights
STATIC = {
    "busy": "◎",          # the o, centred and still
    "tool_running": "◍",  # the o holding its fill
    "done": "✔",
    "error": "✗",
    "warn": "⚠",
    "info": "ℹ",
}

# todo = the dial's three moments
TODO_GLYPHS = {"pending": "○", "in_progress": "◔", "completed": "●"}


def spinner_frames(app=None) -> str:
    """The busy-dial frame set for this terminal (dot-pulse under legacy)."""
    try:
        if app is not None and app.has_class("legacy"):
            return SPINNER_DOT
    except Exception:
        pass
    return SPINNER_DIAL


def spinner_frame(app, index: int) -> str:
    frames = spinner_frames(app)
    return frames[index % len(frames)]


def ripple_frame(timestamp: float) -> str:
    """The thinking ripple at `timestamp` seconds (wall clock keeps every
    thinking block in the same phase — one mind, one ripple)."""
    return RIPPLE[int(timestamp / RIPPLE_INTERVAL) % len(RIPPLE)]
