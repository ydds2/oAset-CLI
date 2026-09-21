"""G0 contracts: the event schema and state snapshot are public surfaces.

The desktop shell will render whatever these produce, so both are pinned:
- the golden event sequence of a scripted turn (adding/removing/reordering
  event types must be a deliberate act that updates this file, and bumps
  schema_version for breaking changes);
- host.snapshot() must carry everything a late-joining view needs, and must
  round-trip messages through the same serialization the session store uses.
"""

from __future__ import annotations

import json

from oaset.agent.messages import Conversation
from oaset.config import default_config
from oaset.host import SessionHost
from oaset.providers import MockProvider, MockToolCall, MockTurn

SCRIPT = [
    MockTurn(content_chunks=["先读文件。\n"],
             tool_calls=[MockToolCall("read_file", {"path": "README.md"})]),
    MockTurn(content_chunks=["结论如下。"], usage={"prompt_tokens": 10, "completion_tokens": 5,
                                                   "total_tokens": 15}),
]

REQUIRED_KEYS = {
    "turn_started": {"user_text"},
    "assistant_delta": {"text"},
    "assistant_message": set(),   # per-message boundary marker
    "tool_call_started": {"id", "name", "arguments"},
    "tool_completed": {"id", "name", "result", "is_error"},
    "turn_completed": {"content", "usage"},
}


async def _collect(host: SessionHost) -> list:
    events = []
    async for event in host.chat("check the repo"):
        events.append(event)
    return events


async def test_golden_event_sequence_is_pinned(workspace):
    host = SessionHost(default_config(), workspace, provider=MockProvider(SCRIPT),
                       model_id="mock/mock-echo", mcp=False, persist=False)
    try:
        events = await _collect(host)
        kinds = [e.type for e in events]
        # THE golden order (captured from the real kernel): per-message
        # boundary markers wrap every assistant message, streamed text
        # arrives as deltas, tool calls pair started/completed, and the turn
        # closes exactly once. Removing/renaming any of these breaks every
        # skin — update this file deliberately and bump schema_version.
        assert kinds == [
            "message_appended",       # the user's own prompt (role-aware)
            "turn_started",
            "assistant_delta",
            "assistant_message",
            "tool_call_started",
            "tool_completed",
            "message_appended",       # the tool result (role-aware)
            "assistant_delta",
            "assistant_message",
            "turn_completed",
        ], kinds
        for event in events:
            required = REQUIRED_KEYS.get(event.type)
            if required:
                assert required <= set(event.data.keys()), (event.type, event.data)
        # every event payload is wire-safe: live Message objects (the
        # in-process convenience on assistant_message) serialize through the
        # same to_dict the session store uses
        def _wire(value):
            if hasattr(value, "to_dict"):
                return value.to_dict()
            raise TypeError(type(value))

        json.dumps([{"type": e.type, **e.data} for e in events],
                   ensure_ascii=False, default=_wire)
    finally:
        await host.aclose()


async def test_snapshot_renders_a_full_view(workspace):
    host = SessionHost(default_config(), workspace, provider=MockProvider(SCRIPT),
                       model_id="mock/mock-echo", mcp=False, persist=False)
    try:
        host.registry.ctx.session_state["todos"] = [
            {"content": "read files", "status": "completed"},
            {"content": "write patch", "status": "in_progress"},
        ]
        await _collect(host)
        snap = host.snapshot()

        assert snap["schema_version"] == 1
        assert snap["model"] == "mock/mock-echo"
        assert snap["mode"] == "default"
        assert snap["message_count"] == len(snap["messages"]) >= 3  # user + tool pair + answer
        assert snap["todos"][1]["status"] == "in_progress"
        assert snap["usage"]["total_tokens"] == 15
        assert json.dumps(snap, ensure_ascii=False)  # fully wire-safe

        # round-trip: snapshot messages rebuild the same conversation shape
        restored = Conversation.from_dicts(snap["messages"])
        assert len(restored.messages) == snap["message_count"]
        assert restored.messages[-1].role == "assistant"
    finally:
        await host.aclose()


# ---------------- K-line pins (kernel/ACP blind review)

def test_lsp_frames_are_pure_crlf_on_windows():
    """The text-mode write translated \n to os.linesep and emitted
    \r\r\n headers — a framing violation on the PRIMARY platform."""
    import io
    import json as _json
    import sys

    from oaset import acp as acp_mod

    class _BytesCapture:
        def __init__(self):
            self.buffer = io.BytesIO()

        def write(self, s):
            self.buffer.write(s.encode("utf-8"))

        def flush(self):
            pass

    payload = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    cap = _BytesCapture()
    real, sys.stdout = sys.stdout, cap
    try:
        acp_mod._write_message(payload, lsp=True)
    finally:
        sys.stdout = real
    raw = cap.buffer.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"Content-Length: ") and b"\r\r" not in raw
    assert _json.loads(body.decode("utf-8"))["id"] == 1


def test_prompt_parts_validates_image_blocks():
    import pytest as _pytest

    from oaset import acp as acp_mod

    with _pytest.raises(KeyError):
        acp_mod._prompt_parts([{"type": "image", "data": "not base64!!"}])
    with _pytest.raises(KeyError):
        acp_mod._prompt_parts([{"type": "image", "data": "aGVsbG8=",
                                "mimeType": "image/png; x\r\nEvil: 1"}])
    with _pytest.raises(KeyError):
        acp_mod._prompt_parts([{"type": "image", "data": {"nested": True}}])
    _text, images = acp_mod._prompt_parts(
        [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}])
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_user_and_tool_messages_are_not_assistant_messages():
    """Role-aware naming: the alias table used to paint the user's own
    prompt (and every tool result) as an assistant message."""
    from oaset.agent.messages import Message
    from oaset.events import normalize_loop_event

    ev = normalize_loop_event({"type": "message_done",
                               "message": Message(role="user", content="hi")},
                              session_id="s", sequence=1)
    assert ev.type == "message_appended"
    ev = normalize_loop_event({"type": "message_done",
                               "message": Message(role="tool", content="out",
                                                  tool_call_id="t1")},
                              session_id="s", sequence=2)
    assert ev.type == "message_appended"
    ev = normalize_loop_event({"type": "message_done",
                               "message": Message(role="assistant", content="ok")},
                              session_id="s", sequence=3)
    assert ev.type == "assistant_message"
