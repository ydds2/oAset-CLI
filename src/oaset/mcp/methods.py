"""MCP method compatibility matrix — the single classification source.

Every standard MCP method is classified by direction and by what oAset does
with it. The matrix drives two behaviours:

1. `classify_message` decides how an incoming server message is treated
   (requests MUST get a response — even "not supported"; notifications are
   accepted and ignored, never answered).
2. The support levels below are the honest public contract: "supported" is
   implemented and tested, "respond-error" means we correctly refuse with
   JSON-RPC -32601, "ignored" means accepted-and-dropped notifications.

Protocol version pinned in client.py: PROTOCOL_VERSION = 2025-06-18.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# support levels
SUPPORTED = "supported"            # implemented + tested
RESPOND_ERROR = "respond-error"    # server→client request answered with -32601
IGNORED = "ignored"                # notification accepted and dropped (by design)

# direction
C2S_REQUEST = "client→server request"
C2S_NOTIFICATION = "client→server notification"
S2C_REQUEST = "server→client request"
S2C_NOTIFICATION = "server→client notification"


@dataclass(frozen=True)
class MethodSpec:
    method: str
    direction: str
    support: str


MATRIX: tuple[MethodSpec, ...] = (
    # lifecycle
    MethodSpec("initialize", C2S_REQUEST, SUPPORTED),
    MethodSpec("notifications/initialized", C2S_NOTIFICATION, SUPPORTED),
    MethodSpec("ping", C2S_REQUEST, SUPPORTED),
    # tools
    MethodSpec("tools/list", C2S_REQUEST, SUPPORTED),
    MethodSpec("tools/call", C2S_REQUEST, SUPPORTED),
    MethodSpec("notifications/tools/list_changed", S2C_NOTIFICATION, IGNORED),
    # resources (read-only listing)
    MethodSpec("resources/list", C2S_REQUEST, SUPPORTED),
    MethodSpec("resources/read", C2S_REQUEST, SUPPORTED),
    MethodSpec("notifications/resources/list_changed", S2C_NOTIFICATION, IGNORED),
    MethodSpec("notifications/resources/updated", S2C_NOTIFICATION, IGNORED),
    MethodSpec("resources/subscribe", C2S_REQUEST, RESPOND_ERROR),
    MethodSpec("resources/unsubscribe", C2S_REQUEST, RESPOND_ERROR),
    # prompts (read-only listing)
    MethodSpec("prompts/list", C2S_REQUEST, SUPPORTED),
    MethodSpec("prompts/get", C2S_REQUEST, SUPPORTED),
    MethodSpec("notifications/prompts/list_changed", S2C_NOTIFICATION, IGNORED),
    # logging / progress (server → client)
    MethodSpec("notifications/message", S2C_NOTIFICATION, IGNORED),          # logging
    MethodSpec("notifications/progress", S2C_NOTIFICATION, IGNORED),
    # server → client requests: sampling/elicitation/roots are real gated
    # capabilities (injected handlers — sampling bridges to the provider,
    # elicitation collects per-field input, roots exposes the workspace root).
    # Without a handler each still answers -32601 rather than hanging.
    MethodSpec("sampling/createMessage", S2C_REQUEST, SUPPORTED),
    MethodSpec("elicitation/create", S2C_REQUEST, SUPPORTED),
    MethodSpec("roots/list", S2C_REQUEST, SUPPORTED),
)

_BY_METHOD = {spec.method: spec for spec in MATRIX}


def spec_for(method: str) -> MethodSpec | None:
    return _BY_METHOD.get(method)


def is_notification(message: dict[str, Any]) -> bool:
    """A JSON-RPC notification has a method and NO id; requests carry an id."""
    return isinstance(message, dict) and "method" in message and "id" not in message


def classify_message(message: dict[str, Any]) -> str:
    """Classify an incoming server message into an action.

    Returns one of: "response" (matched pending request), "request" (server
    request needing a reply), "notification" (drop, never answer),
    "malformed".
    """
    if not isinstance(message, dict):
        return "malformed"
    if "method" not in message:
        # no method + id → response to one of our requests
        return "response" if "id" in message else "malformed"
    if is_notification(message):
        return "notification"
    return "request"


def render_matrix() -> str:
    lines = ["MCP method compatibility (protocol 2025-06-18)"]
    for bucket in (SUPPORTED, RESPOND_ERROR, IGNORED):
        rows = [s for s in MATRIX if s.support == bucket]
        lines.append(f"\n[{bucket}]")
        lines.extend(f"  {s.method:36} {s.direction}" for s in rows)
    return "\n".join(lines)
