"""Canonical event envelope — THE single event vocabulary (Phase B-1).

One frozen dataclass, one vocabulary, one normalizer:

    AgentLoop  ──raw dicts (internal dialect)──▶ normalize_loop_event()
                                                      │
                                                      ▼
                                    Event(schema_version, event_id,
                                          session_id, sequence, timestamp,
                                          type, data)
                                                      │
                              TUI / SDK / CLI / transports (canonical only)

The loop keeps its short internal names (``content_delta``…); they never
leave the kernel — everything crossing the kernel boundary is canonical.
Unknown loop names pass through unchanged so new loop events surface
additively instead of crashing the normalizer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

SCHEMA_VERSION = 1

# The canonical vocabulary. New events are ADDITIVE: consumers must ignore
# types they don't know. Never rename or remove an entry.
CANONICAL_EVENTS: tuple[str, ...] = (
    # session lifecycle
    "session_started",
    "session_closed",
    # turn lifecycle
    "turn_started",
    "turn_completed",
    "message_appended",
    "turn_cancelled",
    "phase_changed",
    # assistant output
    "assistant_delta",
    "reasoning_delta",
    "assistant_message",
    # tools
    "tool_call_started",
    "tool_output_delta",
    "tool_completed",
    "tool_denied",
    # approvals
    "approval_requested",
    "approval_resolved",
    # context / persistence
    "context_planned",
    "context_compacted",
    "auto_compacted",
    "checkpoint_saved",
    "budget_exhausted",
    "iterations_exhausted",
    # provider
    "fallback_started",
    "fallback_completed",
    # plugins / diagnostics
    "plugin_started",
    "plugin_failed",
    "plugin_stopped",
    "diagnostic_created",
    # evidence ledger (/evidence): verification receipts of one turn
    "evidence",
    # generic
    "notice",
    "error",
)

# AgentLoop internal name → canonical name. The loop dialect is INTERNAL;
# this table is the only place that knows about it.
LOOP_EVENT_ALIASES: dict[str, str] = {
    "turn_start": "turn_started",
    "content_delta": "assistant_delta",
    "tool_start": "tool_call_started",
    "tool_progress": "tool_output_delta",
    "tool_end": "tool_completed",
    "message_done": "assistant_message",
    "auto_compact": "auto_compacted",
    "interrupted": "turn_cancelled",
    "turn_done": "turn_completed",
}


def canonical_type(name: str) -> str:
    """Map an internal loop event name to its canonical name (identity when
    the name is already canonical or unknown)."""
    return LOOP_EVENT_ALIASES.get(name, name)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class Event:
    """Immutable canonical event envelope.

    ``data`` carries the event fields WITHOUT ``type`` (use ``payload()`` for
    the flat loop-style view that renderers consume). Tuple iteration and
    ``get()`` keep (type, data) compatibility for earlier SDK clients.
    """

    schema_version: int
    event_id: str
    session_id: str
    sequence: int
    timestamp: str
    type: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __iter__(self) -> Iterator[Any]:
        yield self.type
        yield self.data  # backwards-compatible (type, data) unpacking

    def payload(self) -> dict[str, Any]:
        """Flat view for renderers: {"type": ..., **data}."""
        out: dict[str, Any] = {"type": self.type}
        out.update(self.data)
        return out

    def as_dict(self) -> dict[str, Any]:
        """Full serialisable envelope (the wire/protocol shape)."""
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "type": self.type,
            "data": dict(self.data),
        }


def normalize_loop_event(
    payload: Mapping[str, Any], *, session_id: str, sequence: int
) -> Event:
    """Convert one raw loop emit-dict into a canonical Event.

    Copies the payload so later mutation of the loop's dict cannot rewrite a
    emitted event. Unknown names pass through identity (additive evolution);
    sequences must be non-negative.
    """
    if not isinstance(payload, Mapping) or "type" not in payload:
        raise ValueError(f"loop event without 'type': {payload!r}")
    if not isinstance(sequence, int) or sequence < 0:
        raise ValueError(f"sequence must be a non-negative integer, got {sequence!r}")
    raw_type = str(payload["type"])
    if raw_type == "message_done":
        # role-aware: the loop emits message_done for user/tool/assistant
        # messages alike. Aliasing ALL of them to assistant_message painted
        # the user's own prompt (and tool results) as assistant turns on
        # every consumer that trusts the vocabulary. Only assistant messages
        # keep the assistant_message name; everything else becomes the
        # neutral message_appended (additive — old consumers ignore it).
        message = payload.get("message")
        role = getattr(message, "role", None) or (
            message.get("role") if isinstance(message, dict) else None)
        etype = "assistant_message" if role == "assistant" else "message_appended"
    else:
        etype = canonical_type(raw_type)
    data = {k: v for k, v in payload.items() if k != "type"}
    return Event(
        schema_version=SCHEMA_VERSION,
        event_id=uuid.uuid4().hex,
        session_id=session_id,
        sequence=sequence,
        timestamp=_utc_now_iso(),
        type=etype,
        data=data,
    )


def validate_event_payload(event: Mapping[str, Any]) -> list[str]:
    """Validate a serialized envelope; empty list means valid.

    Unknown types are tolerated (additive protocol); structural violations
    are not."""
    problems: list[str] = []
    if not isinstance(event, Mapping):
        return ["event must be an object"]
    for key in ("schema_version", "event_id", "session_id", "sequence", "timestamp", "type"):
        if key not in event:
            problems.append(f"missing required field: {key}")
    seq = event.get("sequence")
    if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool) or seq < 0):
        problems.append("sequence must be a non-negative integer")
    if event.get("schema_version") not in (None, SCHEMA_VERSION):
        problems.append(f"unsupported schema_version: {event.get('schema_version')!r}")
    data = event.get("data")
    if data is not None and not isinstance(data, Mapping):
        problems.append("data must be an object")
    return problems
