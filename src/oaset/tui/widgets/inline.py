"""Inline prompts docked above the input — pickers, permission confirmation,
login banner and the help card. No modal overlays: everything renders inside
the main column."""

from __future__ import annotations

import asyncio
from typing import Any

from rich.markup import escape
from textual.containers import Vertical
from textual.widgets import Input, Label, Static


class InlinePromptBase(Vertical):
    """Focusable inline panel; subclasses render text and handle keys."""

    can_focus = True
    DEFAULT_CSS = """
    /* Every inline surface (palette, picker, permission, arg form) carries a
    frame: without one the panel blends into the transcript above it. */
    InlinePromptBase { height: auto; padding: 0 1; background: ansi_default;
                       border: round #5f5f5f; }
    /* no soft wrap: _line_map assumes 1 rendered row == 1 option; a wrapped
    label made the click/hover map point at the WRONG option (real repro:
    clicking a path's continuation line selected the path BELOW it) */
    InlinePromptBase .inline-body { text-wrap: nowrap; }
    InlinePromptBase:focus-within, InlinePromptBase:focus { border: round $accent; }
    InlinePromptBase Input { background: ansi_default; }
    InlinePromptBase Input:focus { background: ansi_default; }
    """
    _body: Static

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._future: asyncio.Future | None = None
        # body line index → selectable target (option/match index), rebuilt on
        # every render; mouse handlers resolve clicks through this map
        self._line_map: dict[int, int] = {}

    @property
    def future(self) -> asyncio.Future:
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
        return self._future

    def _resolve(self, value: Any) -> None:
        # The awaiting side (_run_prompt) removes the widget once the future
        # resolves — removal must be awaited there.
        if not self.future.done():
            self.future.set_result(value)
        else:
            # Stale prompt: its awaiting side is gone, so nothing will remove
            # this widget and it keeps swallowing keys and clicks. Hide it here
            # (the app's dismissal then removes it for good).
            self.display = False

    def on_mount(self) -> None:
        self._body = Static("", markup=True, classes="inline-body")
        self.mount(self._body)
        self._refresh()

    def _refresh(self) -> None:  # pragma: no cover - subclass hooks
        self._body.update("")

    def _hint_line(self) -> str:  # pragma: no cover - subclass hooks
        return ""

    def _window(self, lines: list[str], index: int, visible: int = 12) -> list[str]:
        total = len(lines)
        if total <= visible:
            return lines
        start = min(max(0, index - visible // 2), total - visible)
        out: list[str] = []
        if start > 0:
            out.append("[dim]▲ …[/dim]")
        out.extend(lines[start : start + visible])
        if start + visible < total:
            out.append("[dim]▼ …[/dim]")
        return out


class InlineInput(InlinePromptBase):
    """Free-text inline prompt for missing command arguments.

    Composes a native Textual Input (reliable typing/IME/cursor handling);
    resolves with the trimmed value or None (escape/q).

    IME note: the field is a plain Textual ``Input``, so a composition commit
    lands as an ordinary value change. Only the ``Input.Submitted`` event —
    which Textual raises on the Enter that the terminal actually delivers to
    the application — resolves the prompt; a second Enter on an already
    resolved prompt is ignored because the future is done.
    """

    def __init__(self, title: str, placeholder: str = "",
                 password: bool = False, allow_empty: bool = False) -> None:
        self.title_text = title
        self.placeholder_text = placeholder
        self.password = password
        # allow_empty: resolve "" for an empty submission instead of None, so a
        # wizard can tell "Enter keeps the current value" from "user cancelled".
        self.allow_empty = allow_empty
        self._input: Input | None = None
        super().__init__()

    def compose(self):
        yield Label(f"[b]{self.title_text}[/b]")
        yield Input(placeholder=self.placeholder_text, password=self.password,
                    id="inline-input-field")

    def on_mount(self) -> None:
        self._input = self.query_one("#inline-input-field", Input)
        self._input.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self.future.done():
            return  # one Enter == one resolution, even if the terminal repeats it
        value: str | None = event.value.strip()
        if not value and not self.allow_empty:
            value = None
        self._resolve(value)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self._resolve(None)
        elif event.key == "q" and (self._input is None or not self._input.value):
            self._resolve(None)

    async def on_mouse_down(self, event) -> None:
        if event.button != 3:
            return
        import asyncio as _aio

        from oaset.i18n import t
        from oaset.tui import clipboard

        # off the UI thread: a busy clipboard sleeps in retries, and on the
        # loop that froze every stream and timer (same fix as input.py)
        event.stop()
        event.prevent_default()
        text, error = await _aio.to_thread(clipboard.get_text)
        if error is not None:
            code = getattr(error, "code", "")
            if code == "clipboard_unavailable":
                self.app.notify(t("paste_native_hint"), severity="warning")
            elif code != "clipboard_empty":
                self.app.notify(t("paste_empty_warn"), severity="warning")
            return
        if not text:
            self.app.notify(t("paste_empty_warn"), severity="warning")
            return
        if self._input is None:
            self._input = self.query_one("#inline-input-field", Input)
        current = self._input.value or ""
        self._input.value = current + text
        self._input.focus()


class InlinePicker(InlinePromptBase):
    """Single-choice list rendered inline. Resolves with the chosen value or None.

    Readable without a mouse: the title states what is being chosen, each row
    carries its own label, and the footer always shows position, the keys that
    work and how to leave without choosing.
    """

    def __init__(self, title: str, options: list[tuple[str, str]], index: int = 0) -> None:
        self.title_text = title
        self.options = options  # (value, label)
        self.index = index
        super().__init__()

    def _refresh(self) -> None:
        from oaset.i18n import t

        lines = [f"[b]{self.title_text}[/b]"]
        if self.options:
            lines.append(f"[dim]{t('picker_position', index=self.index + 1, total=len(self.options))}[/dim]")
        else:
            lines.append(f"[dim]{t('picker_empty')}[/dim]")
        lines.append("")
        self._line_map = {}
        option_lines = []
        for i, (_value, label) in enumerate(self.options):
            # labels come from workspace file names (path picker): a "[" in a
            # file name is markup to rich, not text — escape everything
            if i == self.index:
                option_lines.append(f"[b]▸ {escape(label)}[/b]")
            else:
                option_lines.append(f"  {escape(label)}")
        windowed = self._window(option_lines, self.index)
        # map rendered rows to option indexes (mirrors _window's slicing)
        total, visible = len(self.options), 12
        if total > visible:
            start = min(max(0, self.index - visible // 2), total - visible)
        else:
            start = 0
        prefix = 1 if start > 0 else 0
        for j in range(min(visible, total - start)):
            self._line_map[len(lines) + prefix + j] = start + j
        lines.extend(windowed)
        lines.append("")
        lines.append(f"[dim]{t('picker_footer')}[/dim]")
        self._body.update("\n".join(lines))

    def _option_at_line(self, y: int) -> int | None:
        return self._line_map.get(int(y))

    def on_mouse_move(self, event) -> None:
        target = self._option_at_line(event.y)
        if target is not None and target != self.index:
            self.index = target
            self._refresh()
            event.stop()

    def on_click(self, event) -> None:
        target = self._option_at_line(event.y)
        if target is None or not self.options:
            return
        self.index = target
        self._refresh()
        self._resolve(self.options[target][0])
        event.stop()

    def on_key(self, event) -> None:
        if not self.options:
            if event.key in ("q", "escape", "enter"):
                self._resolve(None)
                event.prevent_default()
                event.stop()
            return
        if event.key == "down":
            self.index = (self.index + 1) % len(self.options)
        elif event.key == "up":
            self.index = (self.index - 1) % len(self.options)
        elif event.key in ("home", "pagedown", "pageup", "end"):
            self.index = {"home": 0, "end": len(self.options) - 1}.get(
                event.key, self.index)
            if event.key == "pagedown":
                self.index = min(len(self.options) - 1, self.index + 10)
            elif event.key == "pageup":
                self.index = max(0, self.index - 10)
        elif event.key == "enter":
            self._resolve(self.options[self.index][0])
            event.prevent_default()
            event.stop()
            return
        elif event.key in ("q", "escape"):
            self._resolve(None)
            event.prevent_default()
            event.stop()
            return
        else:
            return
        event.prevent_default()
        event.stop()
        self._refresh()


class InlinePermission(InlinePromptBase):
    """Tool confirmation rendered inline. Resolves 'allow' | 'always' | 'deny'.

    The decision shown to the user is the same string the gate receives, and
    every key has a spelled-out label so the choice is readable without
    guessing what 'a' does.
    """

    ALWAYS_LABEL = {
        "read": "permission_always_dir",
        "write": "permission_always",
        "exec": "permission_always",
    }

    def __init__(self, tool: str, level: str, summary: str, preview: str | None = None) -> None:
        self.tool = tool
        self.level = level
        self.summary = summary
        self.preview = preview
        super().__init__()

    def _refresh(self) -> None:
        from oaset.i18n import t

        badge_key = {"write": "badge_write", "exec": "badge_exec",
                     "read": "badge_read"}.get(self.level)
        badge = t(badge_key) if badge_key else str(self.level).upper()
        lines = [t("permission_title", badge=badge), escape(self.summary)]
        if self.preview:
            preview_lines = self.preview.splitlines()
            lines.extend(self._window(["[dim]" + escape(ln) + "[/]" for ln in preview_lines], 0, visible=8))
        always = t(self.ALWAYS_LABEL.get(self.level, "permission_always"))
        lines.append("")
        lines.append(t("permission_keys", always=always))
        self._body.update("\n".join(lines))

    def on_key(self, event) -> None:
        if event.key == "y":
            self._resolve("allow")
        elif event.key == "a":
            self._resolve("always")
        elif event.key in ("n", "escape", "q"):
            self._resolve("deny")


def help_text(*, all_commands: bool = False) -> str:
    """Shortcuts + the daily command surface (or the full registry)."""
    from oaset.i18n import t
    from oaset.tui.commands import (
        PRIMARY_COMMANDS,
        RISK_DANGER,
        RISK_WRITE,
        category_label,
        command_description,
        commands_by_category,
    )
    from oaset.tui.widgets.helpcopy import help_footer, help_header

    header = help_header(all_keys=all_commands)
    footer = help_footer()

    lines: list[str] = [f"[b]{t('help_get_started_title')}[/b]"]
    lines.append(f"  {t('help_get_started')}")
    lines.append("")
    if all_commands:
        lines.append(f"[b]{t('help_commands_title')}[/b]  [dim]{t('help_risk_legend')}[/dim]")
        for category, commands in commands_by_category():
            lines.append(f"[b]{category_label(category)}[/b]")
            for cmd in commands:
                glyph = {RISK_WRITE: "✎", RISK_DANGER: "⚠"}.get(cmd.risk, " ")
                usage = f" {cmd.usage}" if cmd.usage else ""
                aliases = f"  ({'/'.join(cmd.aliases)})" if cmd.aliases else ""
                lines.append(f"  {glyph} /{cmd.name}{usage}{aliases} — {command_description(cmd)}")
    else:
        lines.append(f"[b]{t('help_primary_title')}[/b]  [dim]{t('help_risk_legend')}[/dim]")
        from oaset.tui.commands import COMMANDS

        for cmd in COMMANDS:
            if cmd.name not in PRIMARY_COMMANDS:
                continue
            glyph = {RISK_WRITE: "✎", RISK_DANGER: "⚠"}.get(cmd.risk, " ")
            usage = f" {cmd.usage}" if cmd.usage else ""
            lines.append(f"  {glyph} /{cmd.name}{usage} — {command_description(cmd)}")
        lines.append("")
        lines.append(f"[dim]{t('help_more_hint')}[/dim]")
    return f"{header}\n" + "\n".join(lines) + footer

