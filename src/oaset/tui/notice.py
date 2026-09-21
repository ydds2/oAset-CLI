"""Structured user-facing notices.

Before this existed, every failure path built its own sentence: some had a
machine-readable code, some only a Chinese string, some a bare exception repr.
Nothing told the user what to do next, and the status bar happily overwrote an
error with the next tip.

`UiNotice` is the single shape every level (info/warn/error) goes through:
  text   — one line, already localised
  level  — info | warn | error
  code   — stable id such as "update.fetch_failed" (greppable, testable)
  hint   — the recovery action, or "" when there is nothing useful to say
  source — the subsystem that raised it
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

LEVELS = ("info", "warn", "error")


def clear_pinned_error(app: Any) -> None:
    """Un-pin the status-bar error and forget it for `/errors`.

    The pinned error belongs to the session that produced it: leaving it up
    across `/new`, `/clear` or a session switch kept the bar red with no
    on-screen context, and `/errors` then expanded a failure from a session the
    user had already left.

    Lives here rather than in a controller because it is shared by the session
    and content flows, and controllers must not import each other
    (tests/test_tui_architecture.py).
    """
    app.last_error = None
    with contextlib.suppress(Exception):
        app.status_bar.clear_error()


@dataclass(frozen=True)
class UiNotice:
    text: str
    level: str = "info"
    code: str = ""
    hint: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            object.__setattr__(self, "level", "info")

    @property
    def is_error(self) -> bool:
        return self.level == "error"

    def headline(self) -> str:
        """`[code] text` — the compact form used in the status bar."""
        return f"[{self.code}] {self.text}" if self.code else self.text

    def render(self) -> str:
        return f"{self.headline()}\n→ {self.hint}" if self.hint else self.headline()

    @classmethod
    def from_exception(cls, exc: BaseException, *, source: str = "",
                       text: str = "", hint: str = "") -> UiNotice:
        """Build an error notice from any exception, honouring structured ones.

        `UpdateError`, `MaintenanceError` and `ToolError`-style exceptions carry
        `.code` and `.hint`; anything else degrades to `source.type_name`.
        """
        code = str(getattr(exc, "code", "") or "")
        if not code:
            code = f"{source or 'app'}.{type(exc).__name__}"
        body = text or getattr(exc, "message", None) or str(exc) or type(exc).__name__
        return cls(
            text=f"{type(exc).__name__}: {body}" if not hasattr(exc, "code") else str(body),
            level="error",
            code=code,
            hint=hint or str(getattr(exc, "hint", "") or ""),
            source=source,
        )

    @classmethod
    def of(cls, value: Any, *, level: str = "info", code: str = "",
           hint: str = "", source: str = "") -> UiNotice:
        """Coerce text / exception / UiNotice into a UiNotice."""
        if isinstance(value, UiNotice):
            return value
        if isinstance(value, BaseException):
            return cls.from_exception(value, source=source, hint=hint)
        return cls(str(value), level=level, code=code, hint=hint, source=source)
