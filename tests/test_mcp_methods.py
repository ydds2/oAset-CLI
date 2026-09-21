"""MCP method compatibility matrix and client protocol behaviour."""

from __future__ import annotations

import asyncio
import json

import pytest

from oaset.mcp.client import McpConnection, McpError
from oaset.mcp.methods import (
    IGNORED,
    RESPOND_ERROR,
    SUPPORTED,
    classify_message,
    is_notification,
    render_matrix,
    spec_for,
)


def test_matrix_classifies_all_standard_methods():
    assert spec_for("initialize").support == SUPPORTED
    assert spec_for("tools/call").support == SUPPORTED
    assert spec_for("resources/list").support == SUPPORTED
    assert spec_for("prompts/list").support == SUPPORTED
    # server→client requests: sampling/elicitation/roots are real gated
    # capabilities now
    assert spec_for("sampling/createMessage").support == SUPPORTED
    assert spec_for("elicitation/create").support == SUPPORTED
    assert spec_for("roots/list").support == SUPPORTED
    # notifications are ignored by design — never answered
    assert spec_for("notifications/progress").support == IGNORED
    assert spec_for("notifications/message").support == IGNORED
    assert spec_for("notifications/tools/list_changed").support == IGNORED
    assert spec_for("no/such/method") is None


def test_classify_message_contract():
    assert classify_message({"jsonrpc": "2.0", "id": 1, "result": {}}) == "response"
    assert classify_message({"jsonrpc": "2.0", "id": 5, "method": "sampling/createMessage",
                             "params": {}}) == "request"
    assert classify_message({"jsonrpc": "2.0", "method": "notifications/progress"}) == "notification"
    assert classify_message({"jsonrpc": "2.0"}) == "malformed"
    assert classify_message("garbage") == "malformed"
    assert is_notification({"method": "x"}) is True
    assert is_notification({"method": "x", "id": 1}) is False


def test_render_matrix_lists_every_level():
    text = render_matrix()
    assert SUPPORTED in text and RESPOND_ERROR in text and IGNORED in text
    assert "sampling/createMessage" in text and "tools/call" in text


def _stub_conn() -> McpConnection:
    conn = McpConnection(name="stub", command="unused")
    conn._proc = None  # request() would fail; we intercept below
    return conn


async def test_list_resources_and_prompts(monkeypatch):
    conn = _stub_conn()

    async def fake_request(method, params=None, timeout=None):
        if method == "resources/list":
            return {"resources": [{"uri": "file:///a", "name": "a"}]}
        if method == "prompts/list":
            return {"prompts": [{"name": "p", "description": "d"}]}
        raise AssertionError(method)

    monkeypatch.setattr(conn, "request", fake_request)
    assert (await conn.list_resources())[0]["uri"] == "file:///a"
    assert (await conn.list_prompts())[0]["name"] == "p"


async def test_list_resources_capability_absent_returns_empty(monkeypatch):
    conn = _stub_conn()

    async def refuse(method, params=None, timeout=None):
        raise McpError("stub: Method not found (-32601)")

    monkeypatch.setattr(conn, "request", refuse)
    assert await conn.list_resources() == []
    assert await conn.list_prompts() == []


async def test_list_resources_real_failure_propagates(monkeypatch):
    conn = _stub_conn()

    async def boom(method, params=None, timeout=None):
        raise McpError("stub: connection reset")

    monkeypatch.setattr(conn, "request", boom)
    with pytest.raises(McpError):
        await conn.list_resources()


async def test_read_loop_answers_server_requests_never_notifications(monkeypatch):
    """Server→client requests must get -32601; notifications must not be
    answered at all; responses still resolve pending futures."""
    import asyncio

    conn = _stub_conn()
    written: list[dict] = []

    class FakeStdin:
        def write(self, raw: bytes) -> None:
            written.append(json.loads(raw.decode("utf-8")))

    class FakeProc:
        stdin = FakeStdin()

    conn._proc = FakeProc()  # type: ignore[assignment]

    # pending future resolved by a response frame
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    conn._pending[42] = fut

    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "sampling/createMessage",
                    "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/progress",
                    "params": {"progress": 1}}),
        json.dumps({"jsonrpc": "2.0", "id": 42, "result": {"tools": []}}),
        "not-json",
    ]

    class FakeStdout:
        async def readline(self):
            if lines:
                return (lines.pop(0) + "\n").encode("utf-8")
            return b""

    async def run_read():
        # replicate the loop body import-free: call _read_loop with patched stdout
        conn._proc.stdout = FakeStdout()  # type: ignore[attr-defined]
        await conn._read_loop()

    await asyncio.wait_for(run_read(), timeout=5)
    assert fut.done() and fut.result() == {"tools": []}
    assert len(written) == 1, f"must answer ONLY the request, got: {written}"
    assert written[0]["id"] == 7
    assert written[0]["error"]["code"] == -32601
    assert "sampling/createMessage" in written[0]["error"]["message"]
    assert conn._pending == {}  # response consumed the future


# ------------------------------------------------------------- sampling bridge

async def test_sampling_handler_success_and_denial_paths():

    conn = _stub_conn()
    written: list[dict] = []

    class FakeStdin:
        def write(self, raw: bytes) -> None:
            written.append(json.loads(raw.decode("utf-8")))

    class FakeProc:
        stdin = FakeStdin()

    conn._proc = FakeProc()  # type: ignore[assignment]

    async def run_one(frame: dict, handler) -> dict:
        written.clear()
        conn.sampling_handler = handler
        await conn._serve_sampling(frame)
        assert len(written) == 1
        return written[0]

    ok = await run_one(
        {"jsonrpc": "2.0", "id": 11, "method": "sampling/createMessage", "params": {}},
        _immediate({"content": {"type": "text", "text": "hi"}}),
    )
    assert ok["id"] == 11 and ok["result"]["content"]["text"] == "hi"

    denied = await run_one(
        {"jsonrpc": "2.0", "id": 12, "method": "sampling/createMessage", "params": {}},
        _immediate(None),
    )
    assert denied["error"]["code"] == -32002

    async def boom(params):
        raise RuntimeError("no provider")

    failed = await run_one(
        {"jsonrpc": "2.0", "id": 13, "method": "sampling/createMessage", "params": {}},
        boom,
    )
    assert failed["error"]["code"] == -32603


def _immediate(value):
    async def coro(_params):
        return value
    return coro


async def test_sampling_bridge_gate_and_provider():
    from oaset.mcp.sampling import make_sampling_bridge
    from oaset.providers import MockProvider, MockTurn

    provider = MockProvider([MockTurn(content_chunks=["OK"], finish_reason="stop")])

    class AllowGate:
        async def request(self, tool_name, level, summary, preview=None):
            return "always"

    class DenyGate:
        async def request(self, tool_name, level, summary, preview=None):
            return "deny"

    params = {"messages": [{"role": "user",
                            "content": {"type": "text", "text": "Say OK"}}],
              "maxTokens": 64}

    result = await make_sampling_bridge(provider, AllowGate())(params)
    assert result["content"]["text"] == "OK"
    assert result["role"] == "assistant"
    assert result["stopReason"] == "end_turn"
    assert result["model"] == provider.model_id

    denied = await make_sampling_bridge(provider, DenyGate())(params)
    assert denied is None


async def test_sampling_read_loop_dispatches_to_handler(monkeypatch):
    """Full read-loop path: sampling frame with a handler produces a success
    response; without a handler it still gets -32601 (never dropped)."""

    conn = _stub_conn()
    written: list[dict] = []

    class FakeStdin:
        def write(self, raw: bytes) -> None:
            written.append(json.loads(raw.decode("utf-8")))

    conn._proc = type("P", (), {"stdin": FakeStdin()})()  # type: ignore[assignment]

    async def handler(params):
        return {"role": "assistant",
                "content": {"type": "text", "text": "bridged"},
                "model": "m", "stopReason": "end_turn"}

    conn.sampling_handler = handler
    lines = [json.dumps({"jsonrpc": "2.0", "id": 5,
                         "method": "sampling/createMessage", "params": {}})]

    class FakeStdout:
        async def readline(self):
            if lines:
                return (lines.pop(0) + "\n").encode("utf-8")
            await asyncio.sleep(999)

    async def run_read():
        conn._proc.stdout = FakeStdout()  # type: ignore[attr-defined]
        task = asyncio.ensure_future(conn._read_loop())
        await asyncio.sleep(0.2)  # let the frame + handler round complete
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await asyncio.wait_for(run_read(), timeout=5)
    assert written and written[0]["id"] == 5
    assert written[0]["result"]["content"]["text"] == "bridged"


async def test_sampling_end_to_end_with_fake_server(tmp_path):
    """Real stdio roundtrip: fake server requests sampling right after
    initialized; the bridged client answers through gate+MockProvider."""
    import sys
    import time as _time
    from pathlib import Path

    from oaset.mcp.client import McpConnection
    from oaset.mcp.sampling import make_sampling_bridge
    from oaset.providers import MockProvider, MockTurn

    dump = tmp_path / "sampling_response.json"
    conn = McpConnection(
        name="fake-sampling",
        command=sys.executable,
        args=[str(Path(__file__).parent / "fake_mcp_server.py"), str(dump)],
    )
    provider = MockProvider([MockTurn(content_chunks=["OK"], finish_reason="stop")])

    class AllowGate:
        async def request(self, tool_name, level, summary, preview=None):
            return "always"

    conn.sampling_handler = make_sampling_bridge(provider, AllowGate())
    try:
        await conn.start()  # initialize + notifications/initialized → probe
        dump_sampling = tmp_path / "sampling_response.json.sampling.json"
        deadline = _time.monotonic() + 8
        while _time.monotonic() < deadline and not dump_sampling.exists():
            await asyncio.sleep(0.05)
        assert dump_sampling.exists(), "fake server never received a sampling response"
        frame = json.loads(dump_sampling.read_text(encoding="utf-8"))
        assert frame["id"] == 9001
        assert frame["result"]["content"]["text"] == "OK"
        assert frame["result"]["stopReason"] == "end_turn"
        assert frame["result"]["model"] == provider.model_id
        tools = await conn.list_tools()  # connection still healthy afterwards
        assert any(tool.name == "echo" for tool in tools)
    finally:
        await conn.close()


async def test_sampling_end_to_end_denied(tmp_path):
    import sys
    import time as _time
    from pathlib import Path

    from oaset.mcp.client import McpConnection
    from oaset.mcp.sampling import make_sampling_bridge
    from oaset.providers import MockProvider

    dump = tmp_path / "sampling_denied.json"
    conn = McpConnection(
        name="fake-denied",
        command=sys.executable,
        args=[str(Path(__file__).parent / "fake_mcp_server.py"), str(dump)],
    )

    class DenyGate:
        async def request(self, tool_name, level, summary, preview=None):
            return "deny"

    conn.sampling_handler = make_sampling_bridge(MockProvider([]), DenyGate())
    try:
        await conn.start()
        dump_denied = tmp_path / "sampling_denied.json.sampling.json"
        deadline = _time.monotonic() + 8
        while _time.monotonic() < deadline and not dump_denied.exists():
            await asyncio.sleep(0.05)
        assert dump_denied.exists(), "denied dump never written"
        frame = json.loads(dump_denied.read_text(encoding="utf-8"))
        assert frame["error"]["code"] == -32002
        assert "denied" in frame["error"]["message"]
    finally:
        await conn.close()


# --------------------------------------------------- elicitation / roots

async def test_elicitation_handler_accept_collects_fields():
    from oaset.mcp.bridges import make_elicitation_bridge

    class AllowGate:
        async def request(self, tool_name, level, summary, preview=None):
            return "always"

    async def field_input(title, placeholder):
        return {"Confirm": "yes-please"}.get(title, "typed")

    handler = make_elicitation_bridge(AllowGate(), field_input)
    result = await handler({
        "message": "confirm install",
        "requestedSchema": {"type": "object",
                            "properties": {"answer": {"type": "string",
                                                       "title": "Confirm"}},
                            "required": ["answer"]},
    })
    assert result["action"] == "accept"
    assert result["content"]["answer"] == "yes-please"


async def test_elicitation_gate_denial_returns_none():
    from oaset.mcp.bridges import make_elicitation_bridge

    class DenyGate:
        async def request(self, tool_name, level, summary, preview=None):
            return "deny"

    async def field_input(title, placeholder):  # must never be reached
        raise AssertionError("field input ran despite gate denial")

    handler = make_elicitation_bridge(DenyGate(), field_input)
    assert await handler({"message": "m", "requestedSchema": {}}) is None


async def test_elicitation_user_cancel_maps_to_cancel_action():
    from oaset.mcp.bridges import make_elicitation_bridge

    class AllowGate:
        async def request(self, tool_name, level, summary, preview=None):
            return "allow"

    async def field_input(title, placeholder):
        return None  # user cancelled the required field

    handler = make_elicitation_bridge(AllowGate(), field_input)
    result = await handler({
        "message": "m",
        "requestedSchema": {"type": "object",
                            "properties": {"answer": {"type": "string"}},
                            "required": ["answer"]},
    })
    assert result["action"] == "cancel"


async def test_roots_handler_exposes_workspace_root(tmp_path):
    from oaset.mcp.bridges import make_roots_handler

    class AllowGate:
        async def request(self, tool_name, level, summary, preview=None):
            return "allow"

    handler = make_roots_handler(AllowGate(), tmp_path)
    result = await handler({})
    assert result["roots"][0]["uri"].startswith("file:///")
    assert result["roots"][0]["uri"].endswith(tmp_path.name)


async def test_roots_handler_denied_returns_none(tmp_path):
    from oaset.mcp.bridges import make_roots_handler

    class DenyGate:
        async def request(self, tool_name, level, summary, preview=None):
            return "deny"

    handler = make_roots_handler(DenyGate(), tmp_path)
    assert await handler({}) is None


async def test_read_loop_serves_elicitation_and_roots():
    """Dispatch table routes elicitation/roots to their handlers; the SAME
    method without a handler is refused with -32601."""
    import asyncio

    conn = _stub_conn()
    written: list[dict] = []

    class FakeStdin:
        def write(self, raw: bytes) -> None:
            written.append(json.loads(raw.decode("utf-8")))

    conn._proc = type("P", (), {"stdin": FakeStdin()})()  # type: ignore[assignment]

    async def elicit_handler(params):
        return {"action": "accept", "content": {"answer": "yes"}}

    async def roots_handler(params):
        return {"roots": [{"uri": "file:///ws", "name": "ws"}]}

    conn.elicitation_handler = elicit_handler
    conn.roots_handler = roots_handler

    async def feed(lines):
        class FakeStdout:
            async def readline(self):
                if lines:
                    return (lines.pop(0) + "\n").encode("utf-8")
                return b""

        async def run_read():
            conn._proc.stdout = FakeStdout()  # type: ignore[attr-defined]
            await conn._read_loop()

        await asyncio.wait_for(run_read(), timeout=5)

    await feed([
        json.dumps({"jsonrpc": "2.0", "id": 21, "method": "elicitation/create",
                    "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 22, "method": "roots/list",
                    "params": {}}),
        "not-json",
    ])
    by_id = {frame["id"]: frame for frame in written}
    assert by_id[21]["result"]["content"]["answer"] == "yes"
    assert by_id[22]["result"]["roots"][0]["uri"] == "file:///ws"

    # remove the elicitation handler → the SAME method is now refused
    conn.elicitation_handler = None
    await feed([
        json.dumps({"jsonrpc": "2.0", "id": 23, "method": "elicitation/create",
                    "params": {}}),
    ])
    by_id = {frame["id"]: frame for frame in written}
    assert by_id[23]["error"]["code"] == -32601  # handler absent → refused

