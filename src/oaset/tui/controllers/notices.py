"""Notice & clipboard-copy funnels (P4-1 split from app.py).

Every user-facing message goes through notify_user/notify_error; every
copy goes through _copy_text (Win32 clipboard with retry + OSC52 fallback).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from oaset import __version__
from oaset.i18n import t
from oaset.tui.notice import UiNotice, clear_pinned_error

__all__ = ["NoticesMixin", "clear_pinned_error"]


class NoticesMixin:
    """Notice/copy funnels (verbatim move)."""

    if TYPE_CHECKING:
        # The full UiNotice behind the pinned status-bar error (/errors).
        last_error: UiNotice | None

    def selected_text(self) -> str:
        """Whatever the user selected in the conversation, if anything."""
        try:
            return self.chat.selected_text() if self.chat else ""
        except Exception:
            return ""

    async def action_copy_selection(self) -> None:
        """Ctrl+Shift+C: copy the selection, else the draft (input behaviour)."""
        text = self.selected_text() or self._last_assistant_text() or self.input_area.text
        if not text:
            self.notify_user(UiNotice(t("copy_nothing"), "warn"))
            return
        await self._copy_text(text)

    def _welcome(self) -> None:
        needs_key = self._model_needs_key()
        # the idle tip carousel must not nag "/login" at a mock/local model
        self.status_bar.set_needs_key(needs_key)
        self.chat.add_welcome(
            version=__version__,
            model_id=self.model_cfg.id,
            session_id=self.session.meta.session_id,
            cwd=self.cwd,
            provider=self.model_cfg.provider,
            needs_key=needs_key,
        )
        self.status_bar.set_max_context(self.model_cfg.max_context_size)

    def _refresh_welcome(self) -> None:
        """Remount the welcome page — it painted in a language/theme/credential
        state that has since changed, and its spans are baked at build time."""
        from oaset.tui.widgets.chat import WelcomePanel

        for panel in list(self.chat.query(WelcomePanel)):
            with contextlib.suppress(Exception):
                panel.remove()
        if self._welcomed:
            self._welcome()

    def _notify_done(self, message: str) -> None:
        """Turn-end signal per [ui] notify: bell and/or desktop toast."""
        mode = getattr(self.cfg.ui, "notify", "off")
        if mode not in ("bell", "desktop"):
            return
        if mode == "bell":
            with contextlib.suppress(Exception):
                self.bell()
            return
        # desktop: OSC 9 (Windows Terminal / iTerm2) + OSC 777 (tmux et al)
        with contextlib.suppress(Exception):
            import sys as _sys

            stream = getattr(self, "_notify_stream", None) or _sys.__stdout__
            if stream is None:
                return
            stream.write(f"]9;{message}]777;notify;oAset;{message}")
            stream.flush()

    def _last_assistant_text(self) -> str:
        """Text of the most recent assistant turn in the log (markdown source).

        Includes archived turns: the mounted-DOM cap unmounts old blocks, and
        "/copy last" after a long conversation used to find nothing.
        """
        for turn in reversed(self.chat.assistant_turns()):
            if turn.text().strip():
                return turn.text()
        return ""

    async def _copy_text(self, text: str) -> bool:
        import asyncio as _aio

        from oaset.tui import clipboard

        # set_text retries OpenClipboard with time.sleep — off the UI thread
        error = await _aio.to_thread(clipboard.set_text, text)
        if error is not None:
            # Retry failed / clipboard held by another process: ask the
            # terminal itself (OSC 52, supported by Windows Terminal and the
            # common cross-platform hosts) before giving up.
            with contextlib.suppress(Exception):
                self.copy_to_clipboard(text)
                self.notify_user(t("copied_osc52"), source="clipboard")
                return True
            message, level = clipboard.describe(error)
            self.notify_user(UiNotice(message, level, code=error.code, source="clipboard"))
            return False
        self.notify_user(t("copied_chars", n=len(text)), source="clipboard")
        return True

    def notify_user(self, notice: Any, *, source: str = "") -> UiNotice:
        """Single funnel for every user-facing notice.

        Errors additionally pin their headline to the status bar so a failure
        stays visible instead of being replaced by the rotating tip, and keep
        the full notice for `/errors` to expand in the viewer."""
        resolved = UiNotice.of(notice, source=source)
        self.chat.add_notice(resolved)
        if resolved.is_error:
            self.last_error = resolved
            self.status_bar.set_error(resolved)
        return resolved

    async def cmd_errors(self, args: str) -> None:
        """/errors: the pinned error's FULL detail in the paginated viewer."""
        notice = getattr(self, "last_error", None)
        if notice is None:
            self.chat.add_notice(t("no_pinned_error"), "info")
            return
        from oaset.tui.viewer import view_text

        body = notice.render()
        if notice.hint:
            body += f"\n\nhint: {notice.hint}"
        await view_text(self, f"{t('error_detail_title')} [{notice.code or '-'}]",
                        body)

    def notify_error(self, exc_or_text: Any, *, source: str = "", code: str = "",
                     hint: str = "", text: str = "") -> UiNotice:
        if isinstance(exc_or_text, BaseException):
            notice = UiNotice.from_exception(exc_or_text, source=source, text=text, hint=hint)
        else:
            notice = UiNotice(str(exc_or_text), level="error", code=code,
                              hint=hint, source=source)
        return self.notify_user(notice)
