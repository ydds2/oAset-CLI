"""Stable error codes and public exception hierarchy (Phase B-3).

Every surface (TUI, SDK, CLI, transports) reports failures through these
codes instead of ad-hoc strings, so scripts and embedders can branch on
``error.code`` reliably. Display text stays localised in i18n; codes never
change meaning.
"""

from __future__ import annotations

from typing import Any


class OasetError(Exception):
    """Base class for structured oAset failures.

    ``code`` is the stable machine identifier; ``message`` is human-readable;
    ``hint`` is an optional recovery suggestion; ``retryable`` tells callers
    whether a retry can plausibly succeed; ``details`` carries structured
    context (never secrets).
    """

    code: str = "internal"

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        hint: str = "",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.hint = hint
        self.retryable = retryable
        self.details: dict[str, Any] = dict(details or {})


class TurnCancelledError(OasetError):
    """The turn was cancelled by the user or a shutdown path."""

    code = "cancelled"

    def __init__(self, message: str = "turn cancelled", **kwargs: Any):
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class SessionBusyError(OasetError):
    """A second concurrent turn was rejected for one session (Phase B-5)."""

    code = "session_busy"

    def __init__(self, message: str = "a turn is already running in this session", **kwargs: Any):
        kwargs.setdefault("retryable", True)
        kwargs.setdefault("hint", "wait for the running turn to finish, or cancel it first")
        super().__init__(message, **kwargs)


# Stable codes (keep in one place so docs and tests can enumerate them).
E_SESSION_BUSY = "session_busy"
E_APPROVAL_TIMEOUT = "approval_timeout"
E_APPROVAL_DENIED = "approval_denied"
E_POLICY_DENIED = "policy_denied"
E_TOOL_FAILED = "tool_failed"
E_PROVIDER_UNAVAILABLE = "provider_unavailable"
E_NETWORK_BLOCKED = "network_blocked"
E_CANCELLED = "cancelled"
E_PROTOCOL_INVALID = "protocol_invalid"
E_WORKSPACE_UNTRUSTED = "workspace_untrusted"
