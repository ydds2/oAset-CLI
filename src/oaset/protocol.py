"""本地客户端协议 schema（SDK-01）— Phase B 起为 events.py 的序列化适配器。

本文件不再定义第二份事件词汇：``EVENT_TYPES`` 直接引用
``oaset.events.CANONICAL_EVENTS``，校验基于规范信封字段。
仍保留：ApprovalRequest / ApprovalResponse / Cancel 的纯数据校验，
供 SDK 序列化与未来桌面客户端复用。
"""

from __future__ import annotations

from typing import Any

from oaset.events import (
    CANONICAL_EVENTS,
    SCHEMA_VERSION,
    Event,
    validate_event_payload,
)

PROTOCOL_VERSION = SCHEMA_VERSION

EVENT_TYPES = CANONICAL_EVENTS  # single source: oaset.events

REQUIRED_EVENT_FIELDS = (
    "schema_version",
    "event_id",
    "session_id",
    "sequence",
    "timestamp",
    "type",
)


def validate_event(event: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems; empty means valid."""
    return validate_event_payload(event)


def validate_approval_response(response: dict[str, Any]) -> list[str]:
    if not isinstance(response, dict):
        return ["approval response must be an object"]
    problems: list[str] = []
    if response.get("action_id") is None:
        problems.append("missing action_id")
    if response.get("decision") not in ("allow", "always", "deny"):
        problems.append("decision must be allow/always/deny")
    return problems


def event_to_payload(event: Event) -> dict[str, Any]:
    """Canonical Event → serialisable protocol dict."""
    return event.as_dict()


def event_from_payload(payload: dict[str, Any]) -> Event:
    """Serialised protocol dict → canonical Event (identity when already an
    Event). Structural problems raise ValueError."""
    if isinstance(payload, Event):
        return payload
    problems = validate_event_payload(payload)
    if problems:
        raise ValueError("invalid event payload: " + "; ".join(problems))
    return Event(
        schema_version=payload["schema_version"],
        event_id=payload["event_id"],
        session_id=payload["session_id"],
        sequence=payload["sequence"],
        timestamp=payload["timestamp"],
        type=payload["type"],
        data=dict(payload.get("data") or {}),
    )
