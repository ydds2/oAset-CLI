"""oAset TUI themes — text colours only; the terminal owns the background.

CSS ``transparent`` is *not* the terminal default in Textual/Rich: it becomes
``#000000`` and still paints a solid fill. ``ansi_default`` is the colour that
leaves the console background untouched.
"""

from __future__ import annotations

from textual.theme import Theme

# Terminal default (no fill). Never use CSS "transparent" for the screen.
TERMINAL_BG = "ansi_default"

_NO_FILL = {
    "scrollbar-background": TERMINAL_BG,
    "scrollbar-color": TERMINAL_BG,
    "scrollbar-color-hover": TERMINAL_BG,
    "scrollbar-color-active": TERMINAL_BG,
    "scrollbar-corner-color": TERMINAL_BG,
}

OASET_DARK = Theme(
    name="oaset-dark",
    primary="#5b9bd5",      # brand blue accent
    secondary="#8a93a5",    # dim gray (hints, metadata)
    accent="#e2b93b",       # gold (user messages, spinner)
    foreground="#e6e9ef",
    background=TERMINAL_BG,
    surface=TERMINAL_BG,
    panel=TERMINAL_BG,
    boost=TERMINAL_BG,
    success="#7ec97e",
    warning="#e2b93b",
    error="#e5734f",
    dark=True,
    variables=dict(_NO_FILL),
)

OASET_LIGHT = Theme(
    name="oaset-light",
    primary="#2f6fbf",
    secondary="#5f6a7d",
    accent="#b3831d",
    foreground="#232936",
    background=TERMINAL_BG,
    surface=TERMINAL_BG,
    panel=TERMINAL_BG,
    boost=TERMINAL_BG,
    success="#2e8b3a",
    warning="#b3831d",
    error="#c44a2e",
    dark=False,
    variables=dict(_NO_FILL),
)

THEMES: dict[str, Theme] = {"oaset-dark": OASET_DARK, "oaset-light": OASET_LIGHT}


def load_custom_themes(home=None) -> dict[str, Theme]:
    """User-defined themes from ~/.oaset/themes/*.toml (color keys mirror Theme)."""
    import tomllib
    from pathlib import Path

    base = (home or Path.home()) if home is None else home
    themes_dir = Path(base) / "themes"
    custom: dict[str, Theme] = {}
    if not themes_dir.is_dir():
        return custom
    fields = ("primary", "secondary", "accent", "foreground", "background",
              "surface", "panel", "boost", "success", "warning", "error", "dark",
              "variables")
    from textual.color import Color as _Color

    def _valid_color(value) -> bool:
        if not isinstance(value, str):
            return False
        try:
            _Color.parse(value)
            return True
        except Exception:
            return False

    colors = set(fields) - {"dark", "variables"}
    for path in sorted(themes_dir.glob("*.toml")):
        if path.stem in THEMES:
            # a custom file named oaset-dark.toml must not silently redefine
            # the built-in theme
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        kwargs = {k: data[k] for k in fields if k in data}
        if not kwargs:
            continue
        if any(k in kwargs and not _valid_color(kwargs[k]) for k in colors):
            # Theme is a plain dataclass: an invalid colour only explodes
            # later inside refresh_css, taking the app down
            continue
        if "dark" in kwargs and not isinstance(kwargs["dark"], bool):
            continue
        if "variables" in kwargs and not isinstance(kwargs["variables"], dict):
            continue
        kwargs.setdefault("dark", True)
        custom[path.stem] = Theme(name=path.stem, **kwargs)
    return custom


def register_themes(app, home=None) -> None:
    for theme in (*THEMES.values(), *load_custom_themes(home).values()):
        app.register_theme(theme)


def normalize_theme(name: str | None, extra: (set | frozenset) = frozenset()) -> str:
    if name == "auto" or not name:
        return "oaset-dark"
    return name if name in THEMES or name in extra else "oaset-dark"
