"""oAset brand assets for the terminal — wordmark, gradient, rules.

The identity is one idea carried everywhere: the oAset "o" is the state
(glyphs.py runs it as the motion dial ○◔◑◕●). Static assets here reuse the
Kimi-blue→ice-cyan ramp from themes.py so the wordmark, the hairline rules
and the docs SVG logo read as a single brand.

Everything is plain Rich: no Textual import, so the same assets can render
in the TUI, in probes and in plain-console contexts.
"""

from __future__ import annotations

from rich.text import Text

# ANSI-Shadow block letters, one tuple per letter, all rows equal width.
_LETTERS = {
    "O": (" ██████╗ ",
          "██╔═══██╗",
          "██║   ██║",
          "██║   ██║",
          "╚██████╔╝",
          " ╚═════╝ "),
    "A": (" █████╗ ",
          "██╔══██╗",
          "███████║",
          "██╔══██║",
          "██║  ██║",
          "╚═╝  ╚═╝"),
    "S": ("███████╗",
          "██╔════╝",
          "███████╗",
          "╚════██║",
          "███████╗",
          "╚══════╝"),
    "E": ("███████╗",
          "██╔════╝",
          "█████╗  ",
          "██╔══╝  ",
          "███████╗",
          "╚══════╝"),
    "T": ("████████╗",
          "╚═██╔═╝  ",
          "  ██║    ",
          "  ██║    ",
          "  ██║    ",
          "  ╚═╝    "),
}

WORD = "OASET"
WORDMARK = tuple(
    " ".join(rows) for rows in zip(*(letter for letter in (_LETTERS[c] for c in WORD)))
)
WORDMARK_HEIGHT = len(WORDMARK)
WORDMARK_WIDTH = max(len(line) for line in WORDMARK)

# deep blue → ice cyan; matches themes.py primary (#5b9bd5) mid-ramp
GRADIENT = ("#3b5f9e", "#5b9bd5", "#a8e6ff")
# pale terminals: the ice-cyan tail is invisible on white — run the ramp
# navy → brand blue so the far end stays darker than the background
GRADIENT_LIGHT = ("#17325e", "#2f6fbf", "#5b9bd5")


def truecolor_supported() -> bool:
    """Whether this terminal can render 24-bit colour.

    Glyph degradation (the ``legacy`` class — raster fonts mangle ◔◑◕) and
    colour degradation were once one gate, but a modern conhost (Windows 10
    build 19041+, every Windows 11) renders VT truecolour fine, so cmd.exe
    deserves the gradient too. Only genuinely old consoles get the solid
    wordmark.
    """
    import os
    import sys

    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
        return True
    if sys.platform != "win32":
        return True
    try:
        return sys.getwindowsversion().build >= 19041
    except Exception:
        return False


def _rgb(hex_color: str) -> tuple[int, int, int]:
    value = int(hex_color[1:], 16)
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def gradient_hex(fraction: float, ramp: tuple[str, ...] = GRADIENT) -> str:
    """Colour at `fraction` (0..1) along the two-segment ramp."""
    stops = [_rgb(c) for c in ramp]
    span = (len(stops) - 1) * max(0.0, min(1.0, fraction))
    index = min(int(span), len(stops) - 2)
    local = span - index
    a, b = stops[index], stops[index + 1]
    mixed = tuple(round(a[i] + (b[i] - a[i]) * local) for i in range(3))
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def _gradient_line(line: str, ramp: tuple[str, ...]) -> Text:
    """One line of characters, each column tinted by its position in the ramp.

    Spaces stay unstyled so the gradient covers ink, not air — the eye reads
    the ramp across the glyph strokes.
    """
    text = Text()
    width = max(len(line), 1)
    for column, char in enumerate(line):
        if char == " ":
            text.append(char)
        else:
            text.append(char, style=gradient_hex(column / width, ramp))
    return text


def wordmark(colored: bool = True, ramp: tuple[str, ...] = GRADIENT) -> Text:
    """The block wordmark; colored=False for terminals without truecolor."""
    if not colored:
        return Text("\n".join(WORDMARK), style=ramp[1])
    return Text("\n").join([_gradient_line(line, ramp) for line in WORDMARK])


def rule(width: int, colored: bool = True, ramp: tuple[str, ...] = GRADIENT) -> Text:
    """A hairline ─ rule tinted with the same ramp (a quiet gradient floor)."""
    width = max(width, 1)
    if not colored:
        return Text("─" * width, style=ramp[0])
    return _gradient_line("─" * width, ramp)
