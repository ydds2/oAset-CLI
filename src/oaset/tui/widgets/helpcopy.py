"""Bilingual help copy (headers/footers) shared by the inline help card.

The Keys blocks are generated from `tui/keymap.py` (the single source of
truth, P1-3) at call time — call `help_header()` instead of importing a
constant, so the current language and the live keymap are always what the
user sees.
"""

from __future__ import annotations

_KEY_HEADER_EN = "[b]oAset CLI — shortcuts & commands[/b]\n\n[b]Keys[/b]\n"
_KEY_HEADER_ZH = "[b]oAset CLI —— 快捷键与命令[/b]\n\n[b]按键[/b]\n"
# Daily /help shows the keys a coding CLI user hits every hour; /help all
# dumps the rest (steer, fold, plan strip, editor, F2, clear draft).
_DAILY_KEYS = frozenset({
    "Enter", "Esc", "Ctrl+C ×2", "Shift+Tab", "Ctrl+K",
    "Ctrl+Shift+C/V", "Ctrl+X", "↑/↓", "Right-click",
})


def help_header(*, all_keys: bool = False) -> str:
    """The help-card header: title + the keymap table in the current language."""
    from oaset.i18n import resolve_language
    from oaset.tui.keymap import shortcut_rows

    zh = resolve_language() == "zh"
    rows = shortcut_rows()
    if not all_keys:
        rows = [(keys_, label) for keys_, label in rows if keys_ in _DAILY_KEYS]
    keys = "\n".join(f"  {keys_:<15} {label}" for keys_, label in rows)
    extra = "" if all_keys else (
        "  /help all       完整快捷键与全部命令\n" if zh else
        "  /help all       full keymap and every command\n"
    )
    return (_KEY_HEADER_ZH if zh else _KEY_HEADER_EN) + keys + "\n" + extra


def help_footer() -> str:
    """The permission-model footer in the current language."""
    from oaset.i18n import resolve_language

    if resolve_language() == "zh":
        return HELP_FOOTER_ZH
    return HELP_FOOTER_EN


# Kept for backwards compatibility with older imports; prefer help_header().
HELP_FOOTER_EN = """

[b]Permission model[/b]
  read-only tools run automatically inside the workspace; outside
  reads confirm once per directory; writes and shell commands ask
  for confirmation (y = once, a = this session, n = deny).
"""

HELP_FOOTER_ZH = """

[b]权限模型[/b]
  只读工具在工作区内自动执行；工作区外读取按目录确认；
  写文件与 shell 命令需确认
  （y = 允许一次，a = 本会话允许，n = 拒绝）。
"""
