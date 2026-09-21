"""v0.8.0: Signal/WhatsApp bridge transports + Bedrock/Gemini native providers."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from oaset.config import default_config
from oaset.gateway import GatewayService
from oaset.providers import MockProvider, MockTurn
from oaset.providers.bedrock_native import BedrockNativeProvider
from oaset.providers.gemini_native import GeminiNativeProvider
from oaset.tools import AutoGate, ToolContext, ToolRegistry
from oaset.transports import SignalTransport, WhatsAppTransport

# ------------------------------------------------------- bridge transports


class _BridgeApi(BaseHTTPRequestHandler):
    """Fake Signal/WhatsApp bridge: one long-poll receive, capture sends."""

    sent: list[dict] = []
    polls = 0
    payload: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _BridgeApi.polls += 1
        first = _BridgeApi.polls == 1
        body = json.dumps(_BridgeApi.payload if first else []).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        _BridgeApi.sent.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def _run_bridge_transport(transport, provider):
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(transport.run_forever())

        async def wait_send():
            for _ in range(100):
                if _BridgeApi.sent:
                    break
                await asyncio.sleep(0.05)

        loop.run_until_complete(wait_send())
        loop.run_until_complete(task.close())
    finally:
        loop.close()


class _SignalBridge(BaseHTTPRequestHandler):
    """GET /v1/receive/{account} long-poll + POST /v2/send capture."""

    sent: list[dict] = []
    polls = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        _SignalBridge.polls += 1
        first = _SignalBridge.polls == 1
        payload = ([{"envelope": {"source": "+15550001",
                                  "dataMessage": {"message": "hello signal"}}}] if first else [])
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        _SignalBridge.sent.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def _signal_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SignalBridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_signal_transport_roundtrip(isolated_home, workspace):
    _SignalBridge.sent.clear()
    _SignalBridge.polls = 0
    provider = MockProvider([MockTurn(content_chunks=["signal reply"])])
    service = GatewayService(default_config(), workspace, provider=provider)
    server, port = _signal_server()
    transport = SignalTransport(service, "+15550000",
                                api_base=f"http://127.0.0.1:{port}",
                                allow_send=True, poll_timeout=0)

    async def run():
        task = asyncio.create_task(transport.run_forever())
        for _ in range(100):
            if _SignalBridge.sent:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await transport.close()

    asyncio.run(run())
    server.shutdown()
    server.server_close()
    assert _SignalBridge.sent and "signal reply" in _SignalBridge.sent[0].get("message", "")
    assert transport.events_seen == 1


def test_signal_receive_only_blocks_send(isolated_home, workspace):
    _SignalBridge.sent.clear()
    _SignalBridge.polls = 0
    provider = MockProvider([MockTurn(content_chunks=["r"])])
    service = GatewayService(default_config(), workspace, provider=provider)
    server, port = _signal_server()
    transport = SignalTransport(service, "+15550000",
                                api_base=f"http://127.0.0.1:{port}",
                                allow_send=False, poll_timeout=0)

    async def run():
        task = asyncio.create_task(transport.run_forever())
        for _ in range(60):
            if transport.events_seen:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await transport.close()

    asyncio.run(run())
    server.shutdown()
    server.server_close()
    assert transport.events_seen >= 1
    assert _SignalBridge.sent == []  # 只收不发


def test_whatsapp_bridge_transport_roundtrip(workspace):
    _BridgeApi.sent.clear()
    _BridgeApi.polls = 0
    _BridgeApi.payload = [{"chat_id": "wa-1", "text": "hello whatsapp"}]

    class Handler(_BridgeApi):
        def do_GET(self):
            super().do_GET()

    provider = MockProvider([MockTurn(content_chunks=["whatsapp reply"])])
    service = GatewayService(default_config(), Path("."), provider=provider)

    real_post = _BridgeApi.do_POST

    class RouteHandler(Handler):
        def do_POST(self):
            real_post(self)

    server = ThreadingHTTPServer(("127.0.0.1", 0), RouteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    transport = WhatsAppTransport(service, api_base=f"http://127.0.0.1:{port}",
                                  allow_send=True, poll_timeout=0)

    async def run():
        task = asyncio.create_task(transport.run_forever())
        for _ in range(100):
            if _BridgeApi.sent:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await transport.close()

    asyncio.run(run())
    server.shutdown()
    server.server_close()
    assert _BridgeApi.sent and "whatsapp reply" in json.dumps(_BridgeApi.sent[0])
    assert _BridgeApi.sent[0]["chat_id"] == "wa-1"


# ----------------------------------------------------- bedrock / gemini


def _sse(events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


async def test_gemini_native_full_loop(workspace):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        calls.append(body)
        has_fn_response = any(
            "functionResponse" in part
            for m in body.get("contents", [])
            for part in m.get("parts", [])
        )
        if has_fn_response:
            events = [{
                "candidates": [{"finishReason": "STOP",
                                "content": {"parts": [{"text": "gemini final"}]}}],
                "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 4,
                                  "totalTokenCount": 12},
            }]
        else:
            events = [
                {"candidates": [{"content": {"parts": [
                    {"text": "checking "},
                    {"functionCall": {"name": "read_file", "args": {"path": "a.txt"}}},
                ]}}]},
            ]
        return httpx.Response(200, content=_sse(events), headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiNativeProvider("gemini/gemini-flash", api_key="KEY", client=client)
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=500))
    (workspace / "a.txt").write_text("gemini-note", encoding="utf-8")
    from oaset.agent import AgentLoop, Conversation

    loop = AgentLoop(provider, registry, Conversation(system_prompt="sys"), max_iterations=5)
    events: list[dict] = []
    await loop.run("read a.txt", events.append)

    assert "gemini final" in events[-1]["content"]
    tool_msg = [m for m in loop.conversation.messages if m.role == "tool"][0]
    assert "gemini-note" in tool_msg.content
    assert calls[0]["systemInstruction"]["parts"][0]["text"] == "sys"
    fn_decls = calls[0]["tools"][0]["functionDeclarations"]
    assert any(d["name"] == "read_file" for d in fn_decls)


async def test_gemini_429_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiNativeProvider("gemini/gemini-flash", api_key="k", client=client)
    from oaset.providers.base import ProviderError

    with pytest.raises(ProviderError) as exc:
        async for _ in provider.stream([{"role": "user", "content": "hi"}], None):
            pass
    assert exc.value.retryable is True


def test_bedrock_sigv4_headers_shape():
    from datetime import datetime

    from oaset.providers.bedrock_native import _sigv4_headers

    headers = _sigv4_headers(
        "POST", "bedrock-runtime.us-east-1.amazonaws.com", "/model/m/invoke",
        b"{}", "us-east-1", "AKIA", "secret", session_token="TOKEN",
        now=datetime(2026, 9, 8, 12, 0, 0, tzinfo=None).replace(tzinfo=None),
    )
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIA/20260908/us-east-1/bedrock/")
    assert headers["x-amz-security-token"] == "TOKEN"
    assert "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date" in headers["Authorization"]


async def test_bedrock_native_full_loop(workspace):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        calls.append(body)
        has_result = any(
            part.get("type") == "tool_result"
            for m in body.get("messages", [])
            for part in (m.get("content") if isinstance(m.get("content"), list) else [])
        )
        blocks = (
            [{"type": "text", "text": "bedrock final"}]
            if has_result
            else [
                {"type": "text", "text": "reading"},
                {"type": "tool_use", "id": "toolu_b1", "name": "read_file",
                 "input": {"path": "a.txt"}},
            ]
        )
        stop = "tool_use" if not has_result else "end_turn"
        return httpx.Response(200, json={
            "content": blocks, "stop_reason": stop,
            "usage": {"input_tokens": 11, "output_tokens": 7},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = BedrockNativeProvider(
        "bedrock/claude", access_key="AKIA", secret_key="sec",
        region="us-east-1", client=client,
    )
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=500))
    (workspace / "a.txt").write_text("bedrock-note", encoding="utf-8")
    from oaset.agent import AgentLoop, Conversation

    loop = AgentLoop(provider, registry, Conversation(system_prompt="sys"), max_iterations=5)
    events: list[dict] = []
    await loop.run("read a.txt", events.append)

    assert "bedrock final" in events[-1]["content"]
    tool_msg = [m for m in loop.conversation.messages if m.role == "tool"][0]
    assert "bedrock-note" in tool_msg.content
    assert calls[0]["messages"][0]["role"] == "user"
    assert calls[0]["tools"][0]["input_schema"]["type"] == "object"
    assert "Authorization" not in json.dumps(calls[0])  # auth in headers, not body


# ------------------------------------------- SSE tee: bounded, race-free

def test_subscriber_queue_is_bounded_and_drops_oldest(workspace):
    """A stalled /events reader must not grow the process without end. On
    overflow the OLDEST entry goes — for a live feed the newest matters
    more — and the drop is counted so the stream can announce the gap."""
    import queue as _queue

    service = GatewayService(default_config(), workspace, provider=None)
    q = service.subscribe()
    for i in range(1005):
        service._publish({"type": "progress", "session_id": "s", "i": i})
    assert q.qsize() == 1000
    assert getattr(q, "dropped", 0) >= 5
    first = q.get_nowait()
    last = None
    while True:
        try:
            last = q.get_nowait()
        except _queue.Empty:
            break
    assert first["data"]["i"] == 5, "the oldest entries are the dropped ones"
    assert last["data"]["i"] == 1004, "the newest entry must survive"


def test_subscribe_serializes_against_inflight_publish(workspace):
    """The replay snapshot + registration used to race append + fanout: an
    event published in that window vanished for the new subscriber — a
    panel that silently misses an approval request looks like a hung gate.
    The lock is the contract; hold it and prove subscribe waits."""
    import threading

    service = GatewayService(default_config(), workspace, provider=None)
    service._publish({"type": "seed", "session_id": "s", "data": {}})
    service._events_lock.acquire()
    result: dict = {}

    def racer():
        q = service.subscribe()
        result["q"] = q
        result["items"] = list(q.queue)

    t = threading.Thread(target=racer)
    t.start()
    t.join(0.2)
    assert t.is_alive(), "subscribe must not interleave with a publish"
    service._events_lock.release()
    t.join(2.0)
    assert not t.is_alive()
    # replay kept every event, in order, and flagged it as replay
    items = result["items"]
    assert [e["type"] for e in items] == ["seed"]
    assert items[0].get("replay") is True


def test_panel_broadcast_envelope_still_wraps_dicts(workspace):
    service = GatewayService(default_config(), workspace, provider=None)
    q = service.subscribe()
    service._publish({"type": "approval_requested", "session_id": "s1",
                      "id": "r1", "summary": "rm -rf"})
    import queue as _queue

    entry = q.get_nowait()
    assert entry["type"] == "approval_requested"
    assert entry["data"]["id"] == "r1"
    assert _queue.Empty
