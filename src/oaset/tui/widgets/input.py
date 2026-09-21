"""Input area: multi-line editor, '/'-command and @-file completion, send history."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.timer import Timer
from textual.widgets import TextArea

from oaset.i18n import t
from oaset.tui.notice import UiNotice

if TYPE_CHECKING:
    from oaset.tui.app import OasetApp

PASTE_COLLAPSE_CHARS = 2000
HISTORY_CAP = 200

# An IME commit (Chinese/Japanese composition) arrives as a burst of printable
# keys and, on some Windows terminals, the Enter that confirms the candidate is
# delivered to the app as well. Deferring the send by a beat gives the commit
# time to land and makes "one Enter = at most one send" a hard guarantee: any
# further input cancels the pending send instead of posting a partial string.
SUBMIT_SETTLE_SECONDS = 0.04


def _history_path(app=None):
    # Per WORKSPACE, not per launch directory: `oaset --cwd DIR` changes the
    # app workspace without chdir, so bucketing by Path.cwd() leaked project
    # A's prompts into project B's ↑ recall (and hid B's own history).
    from oaset.utils import oaset_home, workdir_bucket

    workspace = Path.cwd()
    try:
        if app is not None and getattr(app, "cwd", None):
            workspace = Path(app.cwd)
        bucket = workdir_bucket(workspace)
    except Exception:
        bucket = "root"
    return oaset_home() / "input_history" / f"{bucket}.json"


def _load_send_history(app=None) -> list[str]:
    path = _history_path(app)
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()][-HISTORY_CAP:]


def _save_send_history(items: list[str], app=None) -> None:
    import json

    from oaset.utils import atomic_write_text

    path = _history_path(app)
    try:
        # atomic: a crash mid-write left a half file and the next start
        # silently wiped the whole history
        atomic_write_text(path, json.dumps(items[-HISTORY_CAP:], ensure_ascii=False))
    except Exception:
        pass


class InputArea(TextArea):
    """Enter sends; Ctrl+J inserts a newline; ↑/↓ recall history at line edges."""

    DEFAULT_CSS = """
    InputArea {
        background: ansi_default;
        border: none !important;
    }
    InputArea:focus { background: ansi_default; border: none !important; }
    InputArea .text-area--cursor-line, InputArea .text-area--cursor-gutter {
        background: ansi_default;
    }
    """

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    BINDINGS = [
        Binding("enter", "submit", "Send", priority=True),
        Binding("tab", "complete", "Complete", show=False),
        Binding("pageup", "scroll_chat_up", show=False, priority=True),
        Binding("pagedown", "scroll_chat_down", show=False, priority=True),
        Binding("ctrl+j", "newline", "Newline"),
        Binding("ctrl+x", "cut", "Cut", show=False),
        Binding("ctrl+shift+c", "copy_clipboard", "Copy", show=False),
        Binding("ctrl+shift+v", "paste_clipboard", "Paste", show=False),
        Binding("ctrl+v", "paste_clipboard", "Paste", show=False),
        Binding("ctrl+0", "clear_draft", "Clear draft", show=False),
        Binding("up", "cursor_or_history_up", show=False),
        Binding("down", "cursor_or_history_down", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("id", "input")
        kwargs.setdefault("soft_wrap", True)
        super().__init__(**kwargs)
        self.placeholder = ""
        # loaded in on_mount: app (and therefore the workspace bucket) only
        # exists once the widget is mounted
        self.send_history: list[str] = []
        self.send_history_index: int | None = None
        self.draft = ""
        self.suggest_source: Callable[[str], list[tuple[str, str]]] | None = None
        self.file_source: Callable[[str], list[str]] | None = None
        self._suggest_kind = "slash"
        # Placeholders written by insert_collapsed_paste() are turned back into
        # the pasted text at submit time (oaset.tui.paste, called from
        # OasetApp.submit_text). Nothing is injected into the composer here: the
        # box stays small, the turn gets the real content.
        self._suggest_visible = False
        self._suggest_options: list[tuple[str, str]] = []
        self._suggest_index = 0
        self._show_cb: Callable[[list[tuple[str, str]], int], None] = lambda o, i: None
        self._hide_cb: Callable[[], None] = lambda: None
        self._pending_submit: Timer | None = None  # armed by Enter, cancelled by real edits
        self._pending_accept = False  # the armed timer accepts a suggestion (vs sends)
        self._suggest_suppressed_for = ""  # recalled text: Enter must send, not accept
        self._armed_text = ""

    def on_mount(self) -> None:
        # app (and therefore the workspace bucket) only exists once mounted
        self.send_history = _load_send_history(self.app)

    def bind_suggest(self, show, hide) -> None:
        """App supplies render callbacks: show(options, index) / hide()."""
        self._show_cb = show
        self._hide_cb = hide

    # ------------------------------------------------------------- submitting

    def action_submit(self) -> None:
        from textual.screen import ModalScreen

        if isinstance(self.app.screen, ModalScreen):
            return  # a modal is on top; it owns the keyboard
        if self._pending_submit is not None:
            # Second Enter hot on the heels of an armed ACCEPT: the first
            # Enter was a real "accept this" (an IME commit announces itself
            # by CHANGING the buffer, not by pressing Enter again) — flush
            # the accept immediately and let this Enter submit the result.
            if self._pending_accept:
                self._cancel_pending_submit()
                if self._suggest_visible and self.text == self._armed_text:
                    self._accept_suggestion()
                if self.text.strip():
                    self._arm_submit()
            return  # a second Enter while a SEND is pending stays a no-op
        if self._suggest_visible:
            # IME settle: a composition-confirm Enter must not accept the
            # first suggestion before the candidate has finished landing.
            self._armed_text = self.text
            self._pending_accept = True
            self._pending_submit = self.set_timer(
                SUBMIT_SETTLE_SECONDS, self._fire_accept_suggestion)
            return
        if not self.text.strip():
            return
        self._arm_submit()

    def _fire_accept_suggestion(self) -> None:
        self._pending_submit = None
        self._pending_accept = False
        if not self._suggest_visible or self.text != self._armed_text:
            return  # superseded between Enter and the accept
        self._accept_suggestion()

    def _arm_submit(self) -> None:
        """Schedule the send; a second Enter while one is pending is a no-op."""
        if self._pending_submit is not None:
            return
        self._pending_accept = False
        self._armed_text = self.text
        self._pending_submit = self.set_timer(SUBMIT_SETTLE_SECONDS, self._fire_submit)

    def _cancel_pending_submit(self) -> None:
        timer, self._pending_submit = self._pending_submit, None
        self._pending_accept = False
        if timer is not None:
            with contextlib.suppress(Exception):
                timer.stop()

    def on_text_area_changed(self, event) -> None:
        # The buffer actually moved after Enter, so that Enter belonged to an
        # IME composition (or the user kept typing): never post the string it
        # superseded. Compared against the armed snapshot because Textual
        # delivers Changed asynchronously — a message queued *before* Enter
        # must not cancel the send.
        if self._pending_submit is not None and self.text != self._armed_text:
            self._cancel_pending_submit()
        self._refresh_suggest()

    def _fire_submit(self) -> None:
        self._pending_submit = None
        text = self.text.strip()
        if not text or text != self._armed_text.strip():
            return  # superseded or emptied between Enter and the send
        if text in self.send_history:
            self.send_history.remove(text)
        self.send_history.append(text)
        if len(self.send_history) > HISTORY_CAP:
            self.send_history = self.send_history[-HISTORY_CAP:]
        _save_send_history(self.send_history, self.app)
        self.send_history_index = None
        self.draft = ""
        self.post_message(self.Submitted(text))
        self.load_text("")
        self._hide_suggest()

    def action_newline(self) -> None:
        self._cancel_pending_submit()
        self.insert("\n")

    # -------------------------------------------------------------- key route

    def action_cursor_or_history_up(self) -> None:
        if self._suggest_visible:
            self._move_suggestion(-1)
            return
        if self.cursor_at_first_line and self.send_history:
            self._recall(-1)
            return
        self.action_cursor_up()

    def action_cursor_or_history_down(self) -> None:
        if self._suggest_visible:
            self._move_suggestion(1)
            return
        if self.cursor_at_last_line and self.send_history:
            self._recall(1)
            return
        self.action_cursor_down()

    def on_paste(self, event: events.Paste) -> None:
        """Collapse very large pastes into a workspace file reference."""
        text = event.text or ""
        if len(text) <= PASTE_COLLAPSE_CHARS:
            return  # small pastes: let TextArea insert normally
        event.stop()
        event.prevent_default()
        self.insert_collapsed_paste(text)

    def insert_collapsed_paste(self, text: str) -> None:
        """Collapse a huge paste into a file under ~/.oaset, not the project.

        The write is guarded: an unwritable home, a full disk, or lone
        surrogates from the console API (UTF-8 strict rejects them) must
        degrade to an inline insert — never take the app down.
        """
        import uuid as _uuid

        from oaset.tui.paste import prune_pastes
        from oaset.utils import oaset_home

        app = cast("OasetApp", self.app)
        path = oaset_home() / "pastes"
        try:
            path.mkdir(parents=True, exist_ok=True)
            # second-resolution timestamps collide when two big pastes land
            # in the same second: the second overwrote the first and BOTH
            # markers in the draft expanded to the survivor's content
            target = path / (time.strftime("paste-%Y%m%d-%H%M%S")
                             + f"-{_uuid.uuid4().hex[:6]}.txt")
            target.write_text(text, encoding="utf-8")
        except OSError:
            head = text[:2000]
            self.insert(head + ("…" if len(text) > len(head) else " "))
            app.notify_user(UiNotice(
                t("paste_inline_fallback", n=len(text)),
                "warn", code="clipboard.paste_write_failed", source="clipboard"))
            return
        prune_pastes(path)  # scratch files accumulate for as long as the tool is used
        self.insert(f"[pasted: {target} ({len(text)} chars)] ")
        app.notify_user(UiNotice(
            t("paste_collapsed", path=str(target), n=len(text)),
            "info", code="clipboard.collapsed", source="clipboard"))

    async def action_copy_clipboard(self) -> None:
        """Ctrl+Shift+C: the chat selection wins, else the draft (P1-8).

        The old input-focal handler copied the draft unconditionally, which
        silently shadowed the app-level "copy the selection" binding."""
        app = cast("OasetApp", self.app)
        text = app.selected_text() or self.text
        if not text:
            app.notify_user(UiNotice(t("copy_nothing"), "warn", source="clipboard"))
            return
        await app._copy_text(text)

    def _sync_auto_size(self) -> None:
        """Grow with content: a short draft stays 2 rows so the chat keeps
        the screen. Caps at ~40% of the window, and reports the cursor
        position for the status-bar line indicator."""
        try:
            app = cast("OasetApp", self.app)
            logical = max(1, self.document.line_count)
            # Wrapped rows are what the user sees: a 1999-char single-line
            # paste stayed one row tall (only a 1-cell scrollbar hinted at
            # the rest). virtual_size carries the wrap-aware height.
            wrapped = max(logical, int(self.virtual_size.height or 0))
            max_h = max(1, int((app.size.height or 30) * 0.4))
            height = wrapped if wrapped <= 1 else max(2, min(wrapped + 1, max_h))
            if self.styles.height is None or self.styles.height.value != height:
                self.styles.height = height
            app.status_bar.set_input_pos(self.cursor_location[0] + 1, wrapped)
        except Exception:
            pass

    async def _on_mouse_down(self, event) -> None:
        """cmd.exe convention: right-click pastes the clipboard at the cursor.

        Textual dispatches `_on_mouse_down` along the MRO, so the base
        TextArea selection handling runs by itself — an explicit
        `super()._on_mouse_down()` here would run it twice (P0-9: one click
        used to enter TextArea's handler three times)."""
        if event.button == 3:
            with contextlib.suppress(Exception):
                self.focus()
            await self.action_paste_clipboard()
            event.stop()
            event.prevent_default()

    async def action_paste_clipboard(self) -> None:
        """Read the system clipboard through the same large-paste path.

        The read (and a possible CF_DIB→PNG decode of a full-screen screenshot)
        runs off the UI thread: on the loop it froze every stream and timer for
        hundreds of milliseconds per paste.
        """
        import asyncio as _aio

        from oaset.i18n import t
        from oaset.tui import clipboard

        app = cast("OasetApp", self.app)
        png, img_error = await _aio.to_thread(clipboard.get_image_png)
        if png and hasattr(app, "_attach_clipboard_image"):
            await app._attach_clipboard_image(png)
            return
        text, error = await _aio.to_thread(clipboard.get_text)
        if error is not None:
            # A real image decode failure is the actual cause (a screenshot
            # the console cannot decode is NOT an empty clipboard); an
            # image-miss, though, is normal when the clipboard holds text.
            if img_error is not None and img_error.code != "clipboard_empty":
                message, level = clipboard.describe(img_error)
                app.notify_user(UiNotice(message, level, code=img_error.code,
                                         source="clipboard"))
                return
            if error.code == "clipboard_unavailable":
                # POSIX hosts: our win32 reader is unavailable, but the
                # terminal's own paste still delivers text as a Paste event
                app.notify_user(UiNotice(
                    t("paste_native_hint"), "warn",
                    code="clipboard.unavailable", source="clipboard"))
                return
            message, level = clipboard.describe(error)
            app.notify_user(UiNotice(message, level, code=error.code, source="clipboard"))
            return
        if not text:
            # Ctrl+Shift+V on an empty clipboard used to do nothing at all,
            # which reads as "the shortcut is broken"
            cast("OasetApp", self.app).notify_user(
                UiNotice(t("paste_empty_warn"), "warn", code="clipboard.empty",
                         source="clipboard"))
            return
        if len(text) <= PASTE_COLLAPSE_CHARS:
            self.insert(text)
        else:
            self.insert_collapsed_paste(text)

    async def action_cut(self) -> None:
        """Ctrl+X — copy the selection (or the cursor line) and delete it.

        Routed through the app's copy funnel: raw set_text made Ctrl+X dead
        on every non-Windows host while Ctrl+Shift+C worked (the OSC52
        fallback lives in the funnel). Text is only deleted when the copy
        actually succeeded — a failed copy must never eat content.
        """
        text = self.selected_text
        if text:
            text = str(text)  # textual may hand back its own Text type
            start, end = self.selection.start, self.selection.end
        else:
            row = self.cursor_location[0]
            text = str(self.get_line(row))
            start, end = (row, 0), (row, len(text))
        if not text:
            return
        copied = await cast("OasetApp", self.app)._copy_text(text)
        if not copied:
            return  # clipboard failed and no OSC52: keep the text
        self.delete(start, end)

    def action_complete(self) -> None:
        """Tab: accept the highlighted suggestion (never steal focus from the
        composer — the old default moved focus and left the suggest box as an
        ownerless overlay while ↑/↓ silently scrolled the chat)."""
        if self._suggest_visible:
            self._accept_suggestion()
            return
        self.insert("  ")

    def on_blur(self) -> None:
        self._hide_suggest()

    def action_scroll_chat_up(self) -> None:
        chat = getattr(self.app, "chat", None)
        if chat is not None:
            chat.scroll_page_up(animate=False)

    def action_scroll_chat_down(self) -> None:
        chat = getattr(self.app, "chat", None)
        if chat is not None:
            chat.scroll_page_down(animate=False)

    def action_clear_draft(self) -> None:
        from oaset.i18n import t

        self._cancel_pending_submit()
        self.load_text("")
        self.focus()
        cast("OasetApp", self.app).notify_user(t("draft_cleared"), source="input")

    def _recall(self, direction: int) -> None:
        if self.send_history_index is None:
            self.draft = self.text
            self.send_history_index = len(self.send_history)
        self.send_history_index = max(0, min(len(self.send_history), self.send_history_index + direction))
        if self.send_history_index >= len(self.send_history):
            self.send_history_index = None
            self.load_text(self.draft)
        else:
            recalled = self.send_history[self.send_history_index]
            self.load_text(recalled)
            self.move_cursor(self.document.end)
            # A recalled command must be ready to SEND: without this the
            # suggest popup reopened on the same text and the next Enter
            # "accepted" a suggestion (possibly a different command) instead
            # of submitting. Suppression is keyed on the text, so the very
            # next edit re-arms suggestions naturally.
            self._suggest_suppressed_for = recalled
            self._hide_suggest()

    # ----------------------------------------------------------- autocomplete

    def _refresh_suggest(self) -> None:
        if self.suggest_source is None:
            return
        text = self.text
        if text == self._suggest_suppressed_for:
            self._hide_suggest()
            return
        self._suggest_suppressed_for = ""  # the draft moved on; re-arm
        token_start = max(text.rfind(" "), text.rfind("\n")) + 1
        token = text[token_start:]

        # @file mention: complete paths from the workspace
        if token.startswith("@") and self.file_source is not None:
            query = token[1:].replace("\\", "/").lower()
            # The workspace walk (up to ~180 scandir calls) used to run on the
            # UI thread for EVERY keystroke — a hitch per character on big or
            # network repos. Off-thread, with a staleness guard so a slow walk
            # cannot clobber suggestions for newer input.
            import asyncio as _aio

            generation = getattr(self, "_suggest_gen", 0) + 1
            self._suggest_gen = generation

            async def _walk(q: str = query, gen: int = generation) -> None:
                files = await _aio.to_thread(self.file_source, q)
                if gen != getattr(self, "_suggest_gen", 0):
                    return  # superseded by newer input
                if files:
                    self._suggest_kind = "file"
                    self._suggest_options = [(f, "") for f in files]
                    self._suggest_index = 0
                    self._suggest_visible = True
                    self._show_cb(self._suggest_options, 0)
                else:
                    self._hide_suggest()

            self.run_worker(_walk(), group="suggest", exclusive=True)
            return

        if text.startswith("/") and " " not in text and "\n" not in text:
            self._suggest_kind = "slash"
            query = text[1:]
            options = self.suggest_source(query) if self.suggest_source is not None else []
            if options:
                self._suggest_options = options
                self._suggest_index = 0
                self._suggest_visible = True
                self._show_cb(self._suggest_options, 0)
                return
        self._hide_suggest()

    def _move_suggestion(self, delta: int) -> None:
        if not self._suggest_options:
            return
        count = len(self._suggest_options)
        self._suggest_index = (self._suggest_index + delta) % count
        self._show_cb(self._suggest_options, self._suggest_index)

    def _accept_suggestion(self) -> None:
        if not self._suggest_options:
            self._hide_suggest()
            return
        name, _ = self._suggest_options[self._suggest_index]
        if self._suggest_kind == "file":
            # replace the "@query" token with the file path
            text = self.text
            token_start = max(text.rfind(" "), text.rfind("\n")) + 1
            self.load_text(text[:token_start] + name + " ")
        else:
            self.load_text(f"/{name} ")
        self.move_cursor(self.document.end)
        self._hide_suggest()

    def _hide_suggest(self) -> None:
        was = self._suggest_visible
        self._suggest_visible = False
        if was:
            self._hide_cb()
