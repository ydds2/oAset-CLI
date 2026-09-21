"""SDK-01: local client protocol schema validation (Phase B canonical)."""

from __future__ import annotations

import pytest

from oaset.events import CANONICAL_EVENTS, Event, normalize_loop_event
from oaset.protocol import (
    EVENT_TYPES,
    PROTOCOL_VERSION,
    event_from_payload,
    validate_approval_response,
    validate_event,
)


def test_protocol_version_and_event_types_frozen():
    assert PROTOCOL_VERSION == 1
    # protocol vocabulary IS the canonical vocabulary (single source)
    assert EVENT_TYPES is CANONICAL_EVENTS
    for etype in ("turn_started", "tool_call_started", "approval_requested",
                  "turn_completed", "error"):
        assert etype in EVENT_TYPES


def test_validate_event_accepts_minimal_valid():
    event = {"schema_version": 1, "event_id": "e1", "session_id": "s1",
             "sequence": 3, "timestamp": "2026-09-10T00:00:00",
             "type": "turn_started", "data": {}}
    assert validate_event(event) == []


def test_validate_event_reports_missing_fields_and_bad_sequence():
    problems = validate_event({"type": "turn_started", "sequence": -1})
    assert any("event_id" in p for p in problems)
    assert any("sequence" in p for p in problems)


def test_validate_event_unknown_types_are_additive():
    """New event types must pass validation (additive protocol); only the
    structure is enforced."""
    event = {"schema_version": 1, "event_id": "e1", "session_id": "s1",
             "sequence": 0, "timestamp": "t", "type": "mystery_kind", "data": {}}
    assert validate_event(event) == []


def test_validate_event_rejects_non_object():
    assert validate_event(["not", "a", "dict"])


def test_approval_response_validation():
    assert validate_approval_response({"action_id": "a1", "decision": "allow"}) == []
    problems = validate_approval_response({"decision": "maybe"})
    assert any("action_id" in p for p in problems)
    assert any("decision" in p for p in problems)


def test_event_from_payload_roundtrip():
    raw = {"schema_version": 1, "event_id": "e9", "session_id": "s1",
           "sequence": 1, "timestamp": "2026-09-13T00:00:00",
           "type": "assistant_delta", "data": {"text": "hi"}}
    event = event_from_payload(raw)
    assert isinstance(event, Event)
    assert event.type == "assistant_delta"
    assert event.as_dict() == raw


def test_event_from_payload_rejects_incomplete():
    with pytest.raises(ValueError, match="missing required field"):
        event_from_payload({"type": "turn_started", "sequence": 1})


def test_normalize_maps_loop_dialect_to_canonical():
    """Phase B-1 core: loop internal names never leave the kernel."""
    cases = {
        "turn_start": "turn_started",
        "content_delta": "assistant_delta",
        "tool_start": "tool_call_started",
        "tool_end": "tool_completed",
        # message_done is ROLE-aware: with no message attached (this bare
        # payload) it is not an assistant turn — the neutral name applies.
        # The role-aware cases are pinned in test_contract.
        "message_done": "message_appended",
        "auto_compact": "auto_compacted",
        "interrupted": "turn_cancelled",
        "turn_done": "turn_completed",
        "notice": "notice",          # already canonical: identity
        "reasoning_delta": "reasoning_delta",
    }
    for loop_name, canonical in cases.items():
        event = normalize_loop_event({"type": loop_name}, session_id="s", sequence=0)
        assert event.type == canonical
        assert event.schema_version == 1
        assert event.session_id == "s"
        assert "type" not in event.data  # data carries fields, not the name


def test_normalize_rejects_bad_payloads():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        normalize_loop_event({"no_type": 1}, session_id="s", sequence=0)
    with _pytest.raises(ValueError):
        normalize_loop_event({"type": "notice"}, session_id="s", sequence=-1)


def test_envelope_sequence_and_identity_are_per_event():
    first = normalize_loop_event({"type": "content_delta", "text": "a"},
                                 session_id="s", sequence=1)
    second = normalize_loop_event({"type": "turn_done", "content": "a"},
                                  session_id="s", sequence=2)
    assert first.sequence < second.sequence
    assert first.event_id != second.event_id
    assert first.payload() == {"type": "assistant_delta", "text": "a"}
