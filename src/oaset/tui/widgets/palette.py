"""Command palette widgets — a filterable command table plus a confirmation page.

These are deliberately NOT a modal screen: like every other inline panel they
dock above the input in the main column, so the terminal keeps its own scroll
position, focus restoration is handled by the caller's focus stack, and the
same widgets work at 80x24 and 160x50.

Flow implemented here:
  InlineCommandPalette  — type to filter, ↑/↓ to move, enter to choose, esc/q
                          to leave; every row shows category, usage, aliases,
                          source and risk.
  InlineCommandConfirm  — shows the exact command line that would run plus its
                          declared side effect; enter/y runs, esc/q/n goes back.
"""

from __future__ import annotations

from rich.markup import escape

from oaset.tui.commands import effect_display
from oaset.tui.widgets.inline import InlinePromptBase

# Sentinel returned when the user asks for a manual value in a choice form.
CUSTOM_CHOICE = "\x00custom-choice"

RISK_GLYPH = {"safe": "·", "write": "✎", "danger": "⚠"}
RISK_LABEL = {"safe": "read-only", "write": "writes", "danger": "destructive"}


def _fit(text: str, width: int) -> str:
    """Pad or ellipsize plain text to exactly `width` display columns."""
    text = text or ""
    if len(text) > width:
        return text[: max(0, width - 1)] + "…"
    return text.ljust(width)


class InlineCommandPalette(InlinePromptBase):
    """Filterable command table. Resolves with a command name or None.

    The filter is matched against name, description, category and aliases, so
    `/mode`, "permission" and "auto" all reach the same row.

    Rows are nowrap: at 80 columns the widest row wraps, which desynced the
    physical-row mouse map from the logical rows — hovering highlighted and
    clicking RAN the wrong command.
    """

    DEFAULT_CSS = "InlineCommandPalette .inline-body { text-wrap: nowrap; }"

    def __init__(self, commands: list, initial_query: str = "",
                 aliases: dict[str, str] | None = None) -> None:
        self.all_commands = commands
        self.filter_text: str = (initial_query or "").lstrip("/")
        self.aliases = aliases or {}
        self.index = 0
        self.matches: list = []
        self._recent_block: list = []
        super().__init__()

    # -------------------------------------------------------------- filtering

    def _alias_text(self, cmd) -> str:
        aliases = list(cmd.aliases)
        aliases.extend(a for a, target in self.aliases.items() if target == cmd.name)
        return ",".join(sorted(set(aliases)))

    def _haystack(self, cmd) -> str:
        return " ".join((
            cmd.name, cmd.description, cmd.category, self._alias_text(cmd),
            " ".join(cmd.subcommands),
        )).lower()

    def apply_filter(self) -> None:
        from oaset.tui.commands import CATEGORY_ORDER, category_of

        needle = self.filter_text.strip().lower()
        by_category = lambda c: (CATEGORY_ORDER.index(category_of(c)), c.name)  # noqa: E731
        if needle:
            self.matches = [c for c in self.all_commands if needle in self._haystack(c)]
            self.matches.sort(key=by_category)
            self._recent_block = []
        else:
            # Unfiltered view: the commands this user actually runs come first,
            # because the category ordering cannot know their habits.
            from oaset.tui import usage

            recent_names = usage.recent(limit=5)
            by_name = {c.name: c for c in self.all_commands}
            self._recent_block = [by_name[n] for n in recent_names if n in by_name]
            recent_set = {c.name for c in self._recent_block}
            rest = [c for c in self.all_commands if c.name not in recent_set]
            from oaset.tui.commands import PRIMARY_COMMANDS

            rest = [c for c in rest if c.name in PRIMARY_COMMANDS or c.source == "plugin"]
            rest.sort(key=by_category)
            self.matches = [*self._recent_block, *rest]
        # stable category grouping: the list reads as sections, not as one
        # alphabet soup, while search still spans every command
        self.index = max(0, min(self.index, len(self.matches) - 1))

    # --------------------------------------------------------------- rendering

    def _refresh(self) -> None:
        from oaset.i18n import t

        self.apply_filter()
        try:
            cfg = getattr(self.app, "cfg", None)
            reduce_motion = bool(cfg is not None and cfg.ui.reduce_motion)
        except Exception:
            reduce_motion = False
        caret = "▏" if reduce_motion else "[blink]▏[/blink]"
        lines = [f"[b]{t('palette_title')}[/b]  [dim]{t('palette_filter_hint')}[/dim]"]
        lines.append(f"[b]▸[/b] {escape(self.filter_text)}{caret}"
                     if self.filter_text else "[dim]▸ (type to filter)[/dim]")
        if not self.matches:
            lines.append("")
            lines.append(f"[yellow]{t('palette_empty', query=escape(self.filter_text))}[/yellow]")
            lines.append("")
            lines.append(f"[dim]{t('palette_footer')}[/dim]")
            self._body.update("\n".join(lines))
            return

        from oaset.tui.commands import category_label, category_of

        rows: list[str] = []
        row_at: dict[int, int] = {}  # rows index → match index
        current_category = ""
        recent_names = {c.name for c in self._recent_block}
        recent_headed = False
        for i, cmd in enumerate(self.matches):
            category = category_of(cmd)
            if cmd.name in recent_names:
                if not recent_headed:
                    rows.append(f"[dim]── {escape(t('palette_recent'))} ──[/dim]")
                    recent_headed = True
                    current_category = ""  # so the first real section re-headers
            elif category != current_category:
                current_category = category
                rows.append(f"[dim]── {escape(category_label(category))} ──[/dim]")
            row_at[len(rows)] = i
            usage = f" {cmd.usage}" if cmd.usage else ""
            trail = f"{category}{usage}"
            if cmd.subcommands:
                trail += " {" + "|".join(cmd.subcommands) + "}"
            row = (f"{cmd.name:<20}{_fit(trail, 40)}"
                   f"{_fit(self._alias_text(cmd), 14)}{RISK_GLYPH.get(cmd.risk, '·')}")
            if i == self.index:
                rows.append(f"[b]▸ {escape(row)}[/b]")
            else:
                rows.append(f"  {escape(row)}")
        self._line_map = {}
        total_rows, visible = len(rows), 10
        # Window around the SELECTED MATCH'S row, not the match index: the row
        # list carries section headers, so index-as-row drifted the highlight
        # out of the visible window once a few headers stacked up.
        sel_row = next((rows_idx for rows_idx, match_idx in row_at.items()
                        if match_idx == self.index), 0)
        if total_rows > visible:
            start = min(max(0, sel_row - visible // 2), total_rows - visible)
        else:
            start = 0
        prefix = 1 if start > 0 else 0
        base = len(lines)
        for rows_idx, match_idx in row_at.items():
            if start <= rows_idx < start + visible:
                self._line_map[base + prefix + (rows_idx - start)] = match_idx
        lines.extend(self._window(rows, sel_row, visible=visible))

        chosen = self.matches[self.index]
        lines.append("")
        lines.append(t("palette_position", index=self.index + 1, total=len(self.matches)))
        lines.append(f"[b]/{chosen.name}[/b] — {escape(chosen.description)}")
        lines.append(t("palette_details",
                       category=escape(chosen.category),
                       source=escape(chosen.source),
                       risk=RISK_LABEL.get(chosen.risk, chosen.risk),
                       aliases=escape(self._alias_text(chosen) or "—")))
        effect = effect_display(chosen)
        if effect:
            lines.append(f"[dim]{escape(effect)}[/dim]")
        lines.append(f"[dim]{t('palette_footer')}[/dim]")
        self._body.update("\n".join(lines))

    # ------------------------------------------------------------------- keys

    def back(self) -> bool:
        """Unwind one level: clear the filter. False when already unfiltered.

        Called by the app's Escape handler so Escape means "clear, then close"
        instead of throwing the whole palette away on the first press.
        """
        if not self.filter_text:
            return False
        self.filter_text = ""
        self.index = 0
        self._refresh()
        return True

    def on_key(self, event) -> None:
        key = event.key
        if key in ("up", "down") and self.matches:
            step = -1 if key == "up" else 1
            self.index = (self.index + step) % len(self.matches)
        elif key == "pageup" and self.matches:
            self.index = max(0, self.index - 10)
        elif key == "pagedown" and self.matches:
            self.index = min(len(self.matches) - 1, self.index + 10)
        elif key == "enter":
            if self.matches:
                self._resolve(self.matches[self.index].name)
            else:
                self._refresh()  # empty result: stay, the user can fix the filter
            event.prevent_default()
            event.stop()
            return
        elif key in ("escape", "q") and not self.filter_text:
            # Escape is normally consumed by the app's priority interrupt; the
            # direct branch still covers programmatic dispatch in tests.
            self._resolve(None)
            event.prevent_default()
            event.stop()
            return
        elif key == "escape":
            self.back()
        elif key == "backspace":
            self.filter_text = self.filter_text[:-1]
            self.index = 0
        elif key == "ctrl+u":
            self.filter_text = ""
            self.index = 0
        elif getattr(event, "is_printable", False) and event.character:
            self.filter_text += event.character
            self.index = 0
        else:
            return
        event.prevent_default()
        event.stop()
        self._refresh()

    # ------------------------------------------------------------------ mouse

    def _match_at_line(self, y: int) -> int | None:
        return self._line_map.get(int(y))

    def on_mouse_move(self, event) -> None:
        target = self._match_at_line(event.y)
        if target is not None and target != self.index:
            self.index = target
            self._refresh()
            event.stop()

    def on_click(self, event) -> None:
        target = self._match_at_line(event.y)
        if target is None or not self.matches:
            return
        self.index = target
        self._refresh()
        self._resolve(self.matches[target].name)
        event.stop()


class InlineCommandConfirm(InlinePromptBase):
    """Confirmation page: the exact command line and its declared side effect."""

    def __init__(self, command_line: str, effect: str, risk: str) -> None:
        self.command_line = command_line
        self.effect = effect
        self.risk = risk
        super().__init__()

    def _refresh(self) -> None:
        from oaset.i18n import t

        badge = RISK_LABEL.get(self.risk, self.risk)
        lines = [
            t("confirm_title", badge=badge),
            "",
            f"[b]{escape(self.command_line)}[/b]",
        ]
        if self.effect:
            lines.append(f"[dim]{escape(self.effect)}[/dim]")
        lines.append("")
        lines.append(t("confirm_footer"))
        self._body.update("\n".join(lines))

    def on_key(self, event) -> None:
        if event.key in ("y", "enter"):
            self._resolve(True)
        elif event.key in ("n", "escape", "q"):
            self._resolve(False)
