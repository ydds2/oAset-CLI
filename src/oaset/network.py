"""Network egress policy — the product is local-first and receive-only by default.

Modes (config `[network] mode`):
  local_only  — no *remote* I/O. Loopback model servers (Ollama / LM Studio /
                llama.cpp on 127.0.0.1 / ::1) and mock / stdio MCP / local files
                are allowed. Cloud LLM endpoints, web_fetch, HTTP MCP are not.
  pull_only   — DEFAULT. Outbound GET pulls (web_fetch) and model calls to the
                user-configured LLM endpoint are allowed (the product cannot
                reason without them); NO push/send of user content to third
                parties (gateway transports default to receive-only).
  full        — everything, including transport sends (replies) and
                remote-execution backends (modal/daytona/ssh/docker run remote).

"只收不发": the product never uploads sessions/memory/logs anywhere, never
phones home, and push-transports only send when the user explicitly enables it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MODES = ("local_only", "pull_only", "full")

PULL = "pull"
MODEL_CALL = "model_call"
PUSH = "push"
REMOTE_EXEC = "remote_exec"

# what each mode allows
_MATRIX: dict[str, frozenset] = {
    "local_only": frozenset(),
    "pull_only": frozenset({PULL, MODEL_CALL}),
    "full": frozenset({PULL, MODEL_CALL, PUSH, REMOTE_EXEC}),
}

# complete inventory of egress touchpoints in the product (docs contract)
EGRESS_INVENTORY = {
    PULL: ["web_fetch (GET)"],
    MODEL_CALL: ["LLM provider endpoint (user-configured base_url)"],
    PUSH: [
        "gateway transports sendMessage (telegram/discord/slack, opt-in --send-replies)",
        "HTTP hooks (POST the user message / the turn answer to a user-configured URL)",
    ],
    REMOTE_EXEC: [
        "run_shell backends ssh/docker/singularity/modal/daytona (user-configured target)",
    ],
}


class NetworkBlockedError(Exception):
    """Raised when an action is blocked by the network egress policy."""

    def __init__(self, kind: str, mode: str):
        from oaset.i18n import t

        self.kind = kind
        self.mode = mode
        # the refusal is the moment the user must act, so it follows
        # ui_language (the hints used to be Chinese regardless of config)
        hint = {
            ("push", "pull_only"): t("net_blocked_push_pull_only"),
            ("remote_exec", "pull_only"): t("net_blocked_remote_exec_pull_only"),
            ("model_call", "local_only"): t("net_blocked_model_call_local_only"),
        }.get((kind, mode), t("net_blocked_generic", mode=mode, kind=kind))
        super().__init__(hint)
        self.hint = hint


@dataclass
class NetworkPolicy:
    mode: str = "pull_only"

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            self.mode = "pull_only"

    @classmethod
    def from_config(cls, cfg: Any) -> NetworkPolicy:
        mode = getattr(cfg, "network_mode", None) or "pull_only"
        return cls(mode=str(mode))

    def allows(self, kind: str) -> bool:
        return kind in _MATRIX[self.mode]

    def assert_allowed(self, kind: str, *, endpoint: str = "") -> None:
        if self.allows(kind):
            return
        if kind == MODEL_CALL and self.mode == "local_only" and is_loopback_url(endpoint):
            return
        raise NetworkBlockedError(kind, self.mode)


def is_loopback_url(url: str) -> bool:
    """True for mock:// and HTTP(S) URLs whose host is this machine."""
    raw = (url or "").strip().lower()
    if not raw:
        return False
    if raw.startswith("mock://"):
        return True
    from urllib.parse import urlparse

    host = (urlparse(raw).hostname or "").strip("[]")
    return host in ("127.0.0.1", "localhost", "::1")


def normalize_mode(value: str | None) -> str:
    return value if value in MODES else "pull_only"
