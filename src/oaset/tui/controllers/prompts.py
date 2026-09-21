"""Inline-prompt infrastructure (P4-1 split from app.py): pickers, argument
prompts, command forms and the confirm gate. One prompt slot lives on the
app; these methods operate it through `self`."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from textual.containers import Vertical

from oaset.i18n import t
from oaset.tui import commands
from oaset.tui.commands import effect_display
from oaset.tui.widgets.inline import (
    InlineInput,
    InlinePicker,
    InlinePromptBase,
)

_PROMPT_SLOT = "#left"

# sentinel picker row: "type a path that is not in the list" (command flows)
_CUSTOM_PATH = chr(0) + "custom-path"


class PromptsMixin:
    """Picker / arg-form / confirm infrastructure (verbatim move)."""

    if TYPE_CHECKING:
        _active_prompt: InlinePromptBase | None

    def _show_suggest(self, options: list[tuple[str, str]], index: int) -> None:
        widget = self.suggest_box
        visible = 16
        total = len(options)
        if total > visible:
            start = min(max(0, index - visible // 2), total - visible)
        else:
            start = 0
        lines: list[str] = []
        if start > 0:
            lines.append("[dim]▲ …[/dim]")
        for i in range(start, min(start + visible, total)):
            name, desc = options[i]
            # file names from the workspace: escape before they reach markup
            if i == index:
                lines.append(f"[b]▸ {escape(str(name)):<14} {escape(str(desc))}[/b]")
            else:
                lines.append(f"  {escape(str(name)):<14} {escape(str(desc))}")
        if total > visible:
            lines.append(f"[dim]{t('suggest_more', total=total)}[/dim]")
        widget.update("\n".join(lines))
        widget.display = True

    def _hide_suggest(self) -> None:
        widget = self.suggest_box
        widget.display = False

    def _mount_prompt(self, widget: InlinePromptBase) -> None:
        """Dock one inline panel above the input box, superseding any previous.

        Opening a prompt while another is still mounted left the old widget in
        place: it kept focus and swallowed every keystroke. There is one prompt
        slot, and claiming it closes what was there.
        """
        with contextlib.suppress(Exception):
            self.input_area._hide_suggest()  # stale list floated over the panel
        previous = self._active_prompt
        if previous is not None and previous is not widget:
            with contextlib.suppress(Exception):
                # Hide NOW so the superseded panel stops eating clicks/keys;
                # removal stays the awaiting side's job (it needs an await).
                previous.display = False
                if not previous.future.done():
                    previous.future.set_result(None)  # its awaiting side unwinds
            self._forget_prompt(previous)
        self._active_prompt = widget
        # blind-review fix: the input placeholder ("输入消息…") right below an
        # approval panel made it unclear which control owned the keyboard.
        # Silence the input while a prompt owns the dock; restored on dismiss.
        # Stash only the ORIGINAL value: a prompt mounted over another prompt
        # used to stash the already-blanked "" and the original was lost.
        with contextlib.suppress(Exception):
            if getattr(self, "_prompt_placeholder", None) is None:
                self._prompt_placeholder = self.input_box.placeholder
            self.input_box.placeholder = ""
            self.query_one("#prompt-row").display = False
        left = self.query_one("#left", Vertical)
        if not left.is_attached or left._closing:  # teardown race: exit instead of hang
            if not widget.future.done():
                widget.future.set_result(None)
            return
        left.mount(widget, before="#prompt-row")

    def _forget_prompt(self, widget: InlinePromptBase | None) -> None:
        """Drop a prompt from the bookkeeping (removal itself is awaited)."""
        if self._active_prompt is widget:
            self._active_prompt = None

    async def _dismiss_prompt(self, widget: InlinePromptBase | None) -> None:
        """Remove a prompt for good - safe to call twice, safe when stale.

        Escape, a superseding prompt and the awaiting side can all reach this;
        the first one removes the widget, the rest are no-ops.
        """
        if widget is None:
            return
        with contextlib.suppress(Exception):
            if widget.is_mounted:
                widget.display = False  # stop input immediately, even mid-removal
                await widget.remove()
        self._forget_prompt(widget)
        # Restore only when the LAST prompt is gone: an inner prompt dismissing
        # used to re-show the prompt row and restore the placeholder while the
        # outer prompt still owned the dock.
        if self._active_prompt is not None:
            return
        # _dismiss_prompt is deliberately safe to call twice; the restore must
        # be idempotent too, or the second call clobbers the value the first
        # one just put back.
        stashed = getattr(self, "_prompt_placeholder", None)
        if stashed is not None:
            with contextlib.suppress(Exception):
                self.input_box.placeholder = stashed
                self.query_one("#prompt-row").display = True
            self._prompt_placeholder = None

    def _restore_focus(self, previous: Any) -> None:
        """Focus `previous` when it is still usable, else fall back to the input."""
        for candidate in (previous, self.input_box):
            if candidate is None:
                continue
            try:
                if candidate.is_mounted and candidate.can_focus:
                    candidate.focus()
                    return
            except Exception:
                continue

    async def _pick(self, title: str, options: list[tuple[str, str]]) -> str | None:
        """Inline picker docked above the input; resolves with the value or None."""
        return await self._run_prompt(InlinePicker(title, options))

    async def prompt_missing_arg(self, command_name: str) -> str | None:
        """requires_args commands get a structured inline form for the missing
        argument: enum → InlinePicker, path → workspace file picker, model →
        configured-model picker, otherwise free-text InlineInput.

        Every form has a keyboard way out (Esc/q) and a manual-entry fallback,
        and a rejected path re-opens the form with the reason instead of
        dead-ending the command.
        """
        from oaset.tui.commands import arg_spec_for

        spec = arg_spec_for(command_name)
        if spec is None:
            return None
        if spec.kind == "choice" and spec.choices:
            return await self._pick_spec_choice(spec)
        if spec.kind == "model":
            options = [
                (mid, f"{m.display_name or mid} · {mid}")
                for mid, m in sorted(self.cfg.models.items())
            ]
            return await self._pick(spec.title, options) if options else None
        if spec.kind == "path":
            return await self._pick_workspace_path(spec)
        return await self._run_prompt(InlineInput(spec.title, spec.placeholder))

    async def _pick_workspace_path(self, spec) -> str | None:
        """Path picker: filter while walking, then limit; always offer manual entry.

        The old order was `workspace_files(limit=60)` → suffix filter → `[:40]`,
        so a workspace with many non-image files could truncate every valid
        image out of the list before the suffix filter ever ran.
        """
        from oaset.utils import expand_path, fmt_bytes, workspace_files

        suffixes = tuple(spec.filter_suffixes or ())
        while True:
            files = workspace_files(self.cwd, "", limit=40, suffixes=suffixes)
            options: list[tuple[str, str]] = []
            for rel in files:
                label = rel
                try:
                    label = f"{rel}  ·  {fmt_bytes((self.cwd / rel).stat().st_size)}"
                except OSError:
                    pass
                options.append((str(self.cwd / rel), label))
            options.append((_CUSTOM_PATH, t("picker_custom_path")))
            choice = await self._pick(spec.title, options)
            if choice is None:
                return None
            if choice != _CUSTOM_PATH:
                return choice
            raw = await self._run_prompt(InlineInput(t("custom_path_title"), str(self.cwd)))
            if not raw:
                return None
            candidate = expand_path(raw.strip(), self.cwd)
            reason = self._path_rejection(candidate, suffixes)
            if reason is None:
                return str(candidate)
            self.chat.add_notice(reason, "warn")  # recoverable: loop re-opens the form

    async def run_command_flow(self, name: str) -> bool:
        """Drive one command from the palette. Returns True when it ran.

        False means "go back to the palette" — an unhandled name, a cancelled
        parameter form or a declined confirmation. Nothing is executed on any
        of those paths.
        """
        from oaset.tui.commands import arg_spec_for, find_command

        cmd = find_command(name, self.plugin_commands)
        if cmd is None:
            self.chat.add_notice(t("palette_unknown_command", name=name), "warn")
            return False
        handler = (self.plugin_commands.get(cmd.name)
                   or getattr(self, cmd.handler_name, None))
        if handler is None:
            self.chat.add_notice(t("palette_unknown_command", name=cmd.name), "warn")
            return False

        arg = ""
        if cmd.requires_args or arg_spec_for(cmd.name) is not None:
            collected = await self.prompt_missing_arg(cmd.name)
            if not collected:
                return False  # cancelled the form: back to the palette
            arg = collected

        command_line = "/" + cmd.name + (f" {arg}" if arg else "")
        try:
            if cmd.needs_confirmation:
                confirmed = await self._confirm_command(command_line, cmd)
                if not confirmed:
                    return False
                # already confirmed here: the dispatcher must not ask again
                await commands.execute(self, command_line, confirmed=True)
                return True
            await commands.execute(self, command_line)
            return True
        except Exception as exc:
            # the typing path funnels through _on_command_done; the palette
            # path had no guard at all, so one bad plugin killed the app
            with contextlib.suppress(Exception):
                self.notify_error(exc, code="command.crashed", source="command",
                                  hint=t("command_crashed_hint"))
            return False

    async def _confirm_command(self, command_line: str, cmd) -> bool:
        from oaset.tui.widgets.palette import InlineCommandConfirm

        widget = InlineCommandConfirm(command_line, effect_display(cmd), cmd.risk)
        return bool(await self._run_prompt(widget))
