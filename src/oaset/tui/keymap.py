"""Single source of truth for every user-facing shortcut (P1-3 in
docs/PLAN.zh-CN.md).

`/help` and the inline shortcut cheat-sheet render from this table, so a
binding can no longer drift out of the docs (the Ctrl+Shift+C row used to go
missing). App/widget BINDINGS stay literal — Textual needs them — but their
user-facing documentation lives here.

Row shape: (key_label, i18n_key, fallback_english)
"""

KEYMAP: tuple[tuple[str, str, str], ...] = (
    ("Enter", "shortcut_send",
     "send (queued automatically while the agent runs; Ctrl+J = newline)"),
    ("Esc", "shortcut_interrupt",
     "interrupt streaming / close panels / clear the selection"),
    ("Ctrl+C ×2", "shortcut_exit", "exit oAset (with a confirm hint)"),
    ("Shift+Tab", "shortcut_plan", "toggle PLAN mode (read-only)"),
    ("Ctrl+S", "shortcut_steer", "inject an instruction into the running turn"),
    ("Ctrl+O", "shortcut_fold", "fold/unfold tool output & thinking"),
    ("Ctrl+T", "shortcut_sidebar", "toggle the plan strip above the input"),
    ("Ctrl+K", "shortcut_palette", "command palette"),
    ("Ctrl+G", "shortcut_editor", "edit the draft in $EDITOR"),
    ("F2", "shortcut_view",
     "view the last message full-screen (native character selection)"),
    ("Ctrl+Shift+C/V", "shortcut_clipboard",
     "copy (chat selection first, else the draft) / paste"),
    ("Ctrl+X", "shortcut_cut", "cut in the input box (Windows convention)"),
    ("Ctrl+0", "shortcut_clear", "clear the input draft"),
    ("↑/↓", "shortcut_history", "history & suggestion navigation"),
    ("Right-click", "shortcut_rightclick",
     "chat: copy selection / paste (cmd.exe style); input: paste"),
    # Widget-level bindings (chat blocks and tool cards). Their inline
    # `Binding(...)` labels must stay language-neutral because Textual evaluates
    # the table at class-definition time, before the user's language is known —
    # the localised wording lives here instead.
    ("Enter/Space", "shortcut_toggle_block",
     "toggle the focused block/card (also click)"),
)


def shortcut_rows(language: str | None = None) -> list[tuple[str, str]]:
    """(key_label, localised_description) for rendering."""
    from oaset.i18n import t

    return [(keys, t(key)) for keys, key, _fallback in KEYMAP]
