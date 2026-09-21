"""v0.5.0 full-system tests: Discord WS transport, Slack events, Anthropic native
adapter (full agent loop), i18n framework, Modal/Daytona templates."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import websockets

from oaset.agent import AgentLoop, Conversation
from oaset.config import build_provider, default_config, load_config, save_config
from oaset.gateway import GatewayService
from oaset.i18n import set_language, t
from oaset.providers import MockProvider, MockTurn
from oaset.providers.anthropic_native import AnthropicNativeProvider
from oaset.tools import AutoGate, ToolContext, ToolRegistry
from oaset.tools.shell import backend_command_line
from oaset.transports import DiscordTransport, SlackEventsTransport

# ------------------------------------------------------------------ discord


class _FakeDiscordRest(BaseHTTPRequestHandler):
    messages: list[dict] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/gateway/bot":
            body = json.dumps({"url": f"ws://127.0.0.1:{_FakeDiscordRest.ws_port}/gw"}).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/channels/"):
            _FakeDiscordRest.messages.append(body)
            data = json.dumps({"id": "m1"}).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()


async def _fake_discord_gateway(ws, send_json=None):
    """Fake Discord gateway: HELLO -> (on IDENTIFY) READY + MESSAGE_CREATE."""
    import json as j

    hello = {"op": 10, "d": {"heartbeat_interval": 30000}}
    await ws.send(j.dumps(hello))
    identify = j.loads(await ws.recv())
    assert identify["op"] == 2
    assert identify["d"]["token"].startswith("TEST")
    await ws.send(j.dumps({"t": "READY", "s": 1, "op": 0, "d": {"user": {"id": "1"}}}))
    await ws.send(j.dumps({
        "t": "MESSAGE_CREATE", "s": 2, "op": 0,
        "d": {"content": "hi via discord", "channel_id": "77"},
    }))


def test_discord_transport_roundtrip(workspace):
    import asyncio

    rest_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeDiscordRest)
    threading.Thread(target=rest_server.serve_forever, daemon=True).start()
    rest_port = rest_server.server_address[1]
    _FakeDiscordRest.messages.clear()

    async def main():
        ws_server = await websockets.serve(_fake_discord_gateway, "127.0.0.1", 0)
        ws_port = ws_server.sockets[0].getsockname()[1]
        cfg = default_config()
        provider = MockProvider([MockTurn(content_chunks=["discord reply"])])
        service = GatewayService(cfg, workspace, provider=provider)
        transport = DiscordTransport(
            service, "TESTTOKEN", api_base=f"http://127.0.0.1:{rest_port}",
            gateway_override=f"ws://127.0.0.1:{ws_port}/gw", allow_send=True,
        )
        task = asyncio.create_task(transport.run_forever())
        try:
            for _ in range(100):
                if _FakeDiscordRest.messages:
                    break
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            ws_server.close()
            await ws_server.wait_closed()
            await transport.close()

    asyncio.run(main())
    rest_server.shutdown()
    rest_server.server_close()
    assert _FakeDiscordRest.messages, "bot should reply through the REST API"
    assert _FakeDiscordRest.messages[0]["content"] == "discord reply"


# -------------------------------------------------------------------- slack


def test_slack_url_verification_and_event_callback(workspace):
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"path": request.url.path, "body": json.loads(request.read())})
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = default_config()
    provider = MockProvider([MockTurn(content_chunks="slack reply")])
    service = GatewayService(cfg, workspace, provider=provider)
    transport = SlackEventsTransport(service, "xoxb-test", client=client, allow_send=True)

    async def run():
        challenge = await transport.handle_payload({"type": "url_verification", "challenge": "xyz"})
        assert challenge == {"challenge": "xyz"}
        await transport.handle_payload({
            "type": "event_callback",
            "event": {"type": "message", "channel": "C1", "text": "hi slack", "bot_id": None},
        })

    asyncio.run(run())
    posts = [c for c in captured if c["path"].endswith("/chat.postMessage")]
    assert posts and posts[0]["body"]["text"] == "slack reply"
    assert posts[0]["body"]["channel"] == "C1"


# ------------------------------------------------------- anthropic native


def _anthropic_sse_events() -> bytes:
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 12, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Let me read"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '{"path": "a.txt"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": 9}},
    ]
    return b"".join(f"event: x\ndata: {json.dumps(e)}\n\n" for e in events)


async def test_anthropic_native_full_loop(workspace):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        calls.append(body)
        saw_tool_result = any(
            isinstance(part, dict) and part.get("type") == "tool_result"
            for m in body.get("messages", [])
            for part in (m.get("content") if isinstance(m.get("content"), list) else [])
        )
        if saw_tool_result:
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 20, "output_tokens": 0}}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "final answer reached"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"output_tokens": 5}},
            ]
        else:
            events = None
            # first call: tool use with thinking
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 12, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "thinking", "thinking": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "thinking_delta", "thinking": "ponder"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "content_block_start", "index": 1,
                 "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}},
                {"type": "content_block_delta", "index": 1,
                 "delta": {"type": "input_json_delta", "partial_json": '{"path": "a.txt"}'}},
                {"type": "content_block_stop", "index": 1},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                 "usage": {"output_tokens": 9}},
            ]
        sse = "".join(f"event: x\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicNativeProvider(
        "anthropic/claude-sonnet", api_key="sk", client=client
    )
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=500))
    (workspace / "a.txt").write_text("anthropic-note", encoding="utf-8")
    loop = AgentLoop(provider, registry, Conversation(system_prompt="sys"), max_iterations=5)
    events: list[dict] = []
    await loop.run("read a.txt", events.append)

    roles = [m.role for m in loop.conversation.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    tool_msg = [m for m in loop.conversation.messages if m.role == "tool"][0]
    assert "anthropic-note" in (tool_msg.content or "")
    # provider-level conversion: tool result -> Anthropic tool_result block
    second_call_messages = calls[1]["messages"]
    tool_results = [
        part
        for m in second_call_messages
        for part in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(part, dict) and part.get("type") == "tool_result"
    ]
    assert tool_results and "anthropic-note" in tool_results[0]["content"]
    assert calls[0]["system"] == "sys"
    assert calls[0]["tools"][0]["input_schema"]["type"] == "object"


async def test_anthropic_401_classification():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='{"error":{"message":"bad key"}}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicNativeProvider("anthropic/claude-sonnet", api_key="bad", client=client)
    from oaset.providers.base import ProviderError

    with pytest.raises(ProviderError) as exc:
        async for _ in provider.stream([{"role": "user", "content": "hi"}], None):
            pass
    assert exc.value.category == "auth"


def test_anthropic_type_routes_in_config(isolated_home):
    cfg = default_config()
    provider = build_provider(cfg, cfg.models["anthropic/claude-sonnet"])
    assert type(provider).__name__ == "AnthropicNativeProvider"


# ------------------------------------------------------------------- i18n


def test_i18n_catalog_and_fallback():
    assert t("welcome_title", "en") == "oAset"
    assert t("welcome_title", "zh") == "oAset"
    set_language("zh")
    assert t("welcome_title") == "oAset"
    set_language("en")
    assert t("welcome_title") == "oAset"
    assert t("no_such_key_at_all") == "no_such_key_at_all"  # unknown key passthrough
    set_language("zh")
    line = t("context_line", pct=3.2, used="4.1k", total="128.0k")
    assert "3.2%" in line and "128.0k" in line
    set_language("en")


def test_ui_language_config_roundtrip(isolated_home):
    cfg = default_config()
    cfg.ui_language = "zh"
    save_config(cfg)
    assert load_config().ui_language == "zh"


# --------------------------------------------------------------- backends


def test_modal_daytona_templates():
    assert backend_command_line("x", {"backend": "modal", "modal_container": "app"}) == [
        "modal", "container", "exec", "app", "bash", "-c", "x"]
    assert backend_command_line("x", {"backend": "daytona", "daytona_target": "sb"}) == [
        "daytona", "exec", "sb", "--", "bash", "-c", "x"]
    with pytest.raises(ValueError):
        backend_command_line("x", {"backend": "modal"})
    with pytest.raises(ValueError):
        backend_command_line("x", {"backend": "daytona"})
