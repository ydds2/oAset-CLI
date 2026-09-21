"""Gateway transports: Telegram long-polling over the same
GatewayService core. More transports plug in as small classes like this one."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx

from oaset.gateway import GatewayService


class TelegramTransport:
    """Long-polling Telegram bot transport.

    Token resolution: constructor argument > TELEGRAM_BOT_TOKEN env > stored
    credential 'telegram' (/login or `oaset login telegram`).
    api_base is overridable so tests run against a local fake Bot API.
    """

    def __init__(
        self,
        service: GatewayService,
        token: str,
        api_base: str = "https://api.telegram.org",
        poll_timeout: int = 25,
        client: httpx.AsyncClient | None = None,
        allow_send: bool = False,
    ):
        self.service = service
        self.token = token
        self.allow_send = allow_send
        self.api_base = api_base.rstrip("/")
        self.poll_timeout = poll_timeout
        self._client = client or httpx.AsyncClient(timeout=40)
        self._offset = 0
        self.polls = 0
        self.events_seen = 0

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        # Telegram's `result` shape varies per method (list for getUpdates,
        # dict/bool for sendMessage) — typing it as dict here made callers
        # iterate dict keys in type-space while the wire sends a list.
        resp = await self._client.post(f"{self.api_base}/bot{self.token}/{method}", json=payload or {})
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {data}")
        return data["result"]

    async def run_forever(self) -> None:
        while True:
            try:
                updates = await self._call(
                    "getUpdates",
                    {"offset": self._offset, "timeout": self.poll_timeout,
                     "allowed_updates": ["message"]},
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(3)  # transient network error — keep polling
                continue
            self.polls += 1
            for update in updates:
                self.events_seen += 1
                self._offset = max(self._offset, update.get("update_id", 0) + 1)
                message = update.get("message") or {}
                chat_id = (message.get("chat") or {}).get("id")
                text = (message.get("text") or "").strip()
                if chat_id is None or not text:
                    continue
                result = await self.service.handle_message(text)
                reply = result.get("reply") or f"error: {result.get('error', 'unknown')}"
                if self.allow_send:
                    with contextlib.suppress(Exception):
                        await self._call("sendMessage", {"chat_id": chat_id, "text": reply[:4000]})
            await asyncio.sleep(0.05)  # yield to the loop: empty polls must not starve it

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()


class DiscordTransport:
    """Discord bot transport — WebSocket gateway + REST, mirroring Telegram's shape.

    Gateway URL: `gateway_override` (tests point this at a local fake gateway)
    or GET {api_base}/gateway/bot. Handles HELLO → IDENTIFY → heartbeats and
    MESSAGE_CREATE dispatch; replies through POST /channels/{id}/messages.
    """

    INTENTS = 512 | 32768  # GUILD_MESSAGES | MESSAGE_CONTENT

    def __init__(
        self,
        service: GatewayService,
        token: str,
        api_base: str = "https://discord.com/api/v10",
        gateway_override: str | None = None,
        client: httpx.AsyncClient | None = None,
        ws_connect=None,
        allow_send: bool = False,
    ):
        self.service = service
        self.token = token
        self.allow_send = allow_send
        self.api_base = api_base.rstrip("/")
        self.gateway_override = gateway_override
        self._client = client or httpx.AsyncClient(timeout=30)
        self._ws_connect = ws_connect  # injection point for tests
        self.events_seen = 0

    async def _resolve_gateway(self) -> str:
        if self.gateway_override:
            return self.gateway_override
        resp = await self._client.get(
            f"{self.api_base}/gateway/bot",
            headers={"Authorization": f"Bot {self.token}"},
        )
        return resp.json()["url"]

    async def _rest(self, method: str, path: str, payload: dict | None = None) -> dict:
        resp = await self._client.request(
            method,
            f"{self.api_base}{path}",
            json=payload,
            headers={"Authorization": f"Bot {self.token}"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"discord {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except Exception:
            return {}

    async def run_forever(self) -> None:
        import websockets

        while True:
            try:
                gateway = await self._resolve_gateway()
                async with websockets.connect(f"{gateway}?v=10&encoding=json") as ws:
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(3)  # transient gateway error — reconnect
                continue

    async def _session(self, ws) -> None:
        import json as _json

        hello = _json.loads(await ws.recv())
        if hello.get("op") != 10:
            raise RuntimeError(f"expected HELLO, got op={hello.get('op')}")
        interval = (hello.get("d", {}).get("heartbeat_interval") or 30000) / 1000.0

        async def heartbeat():
            while True:
                await asyncio.sleep(interval)
                with contextlib.suppress(Exception):
                    await ws.send(_json.dumps({"op": 1, "d": None}))

        beat = asyncio.create_task(heartbeat())
        try:
            await ws.send(_json.dumps({
                "op": 2,
                "d": {
                    "token": self.token,
                    "intents": self.INTENTS,
                    "properties": {"os": "any", "browser": "oaset", "device": "oaset"},
                },
            }))
            async for raw in ws:
                event = _json.loads(raw)
                name = event.get("t")
                data = event.get("d") or {}
                if name == "MESSAGE_CREATE":
                    self.events_seen += 1
                    content = (data.get("content") or "").strip()
                    channel_id = data.get("channel_id")
                    if not content or channel_id is None:
                        continue
                    result = await self.service.handle_message(content)
                    reply = result.get("reply") or f"error: {result.get('error', 'unknown')}"
                    if self.allow_send:
                        await self._rest(
                            "POST", f"/channels/{channel_id}/messages", {"content": reply[:2000]}
                        )
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()


class SlackEventsTransport:
    """Slack Events API transport over HTTP.

    Handles `url_verification` (challenge echo) and `event_callback`
    (app_mention/message) — replies through chat.postMessage. Point
    `api_base` at a local fake Slack for offline tests.
    """

    def __init__(
        self,
        service: GatewayService,
        bot_token: str,
        api_base: str = "https://slack.com/api",
        client: httpx.AsyncClient | None = None,
        allow_send: bool = False,
    ):
        self.service = service
        self.bot_token = bot_token
        self.allow_send = allow_send
        self.api_base = api_base.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=30)
        self.events_seen = 0

    async def _post_message(self, channel: str, text: str) -> None:
        resp = await self._client.post(
            f"{self.api_base}/chat.postMessage",
            json={"channel": channel, "text": text[:4000]},
            headers={"Authorization": f"Bearer {self.bot_token}"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"slack chat.postMessage -> HTTP {resp.status_code}")

    async def handle_payload(self, payload: dict) -> dict:
        kind = payload.get("type")
        if kind == "url_verification":
            return {"challenge": payload.get("challenge", "")}
        if kind == "event_callback":
            event = payload.get("event") or {}
            if event.get("type") in ("message", "app_mention") and not event.get("bot_id"):
                text = (event.get("text") or "").strip()
                channel = event.get("channel")
                if text and channel:
                    self.events_seen += 1
                    result = await self.service.handle_message(text)
                    reply = result.get("reply") or f"error: {result.get('error', 'unknown')}"
                    if self.allow_send:
                        await self._post_message(channel, reply)
            return {"ok": True}
        return {"ok": True}

    async def serve_http(self, host: str = "0.0.0.0", port: int = 3000) -> None:
        """Blocking HTTP listener for Slack's Events API."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        transport = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                if self.path.rstrip() != "/slack/events":
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("content-length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = asyncio.run(transport.handle_payload(payload))
                body = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer((host, port), Handler)
        server.serve_forever()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()


class SignalTransport:
    """signal-cli REST daemon transport — fully local bridge (local-first).

    Bridge: signal-cli-rest-api (self-hosted, no paid cloud). Receives via
    GET /v1/receive/{account} (long poll); sends via POST /v2/send
    {"number": account, "recipients": [peer], "message": text}.
    """

    def __init__(
        self,
        service: GatewayService,
        account: str,
        api_base: str = "http://127.0.0.1:8080",
        client: httpx.AsyncClient | None = None,
        allow_send: bool = False,
        poll_timeout: int = 25,
    ):
        self.service = service
        self.account = account
        self.api_base = api_base.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=40)
        self.allow_send = allow_send
        self.poll_timeout = poll_timeout
        self.polls = 0
        self.events_seen = 0

    async def _call(self, method_path: str, payload: dict | None = None):
        if payload is None:
            resp = await self._client.get(f"{self.api_base}{method_path}")
        else:
            resp = await self._client.post(f"{self.api_base}{method_path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def run_forever(self) -> None:
        while True:
            try:
                envelopes = await self._call(
                    f"/v1/receive/{self.account}?timeout={self.poll_timeout}"
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(3)
                continue
            self.polls += 1
            await asyncio.sleep(0.05)  # yield: empty polls must not starve the loop
            for envelope in envelopes or []:
                data = envelope.get("envelope", {})
                message = (data.get("dataMessage") or {}).get("message", "").strip()
                source = data.get("source", "")
                if not message or not source:
                    continue
                self.events_seen += 1
                result = await self.service.handle_message(message)
                reply = result.get("reply") or f"error: {result.get('error', 'unknown')}"
                if self.allow_send:
                    with contextlib.suppress(Exception):
                        await self._call(
                            "/v2/send",
                            {"number": self.account, "recipients": [source], "message": reply[:4000]},
                        )

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()


class WhatsAppTransport:
    """Self-hosted WhatsApp bridge transport (bridge exposes a tiny HTTP API).

    Bridge contract (any implementation):
      GET  {api_base}/receive?timeout=N  -> [{"chat_id": "...", "text": "..."}]
      POST {api_base}/send               -> {"chat_id": "...", "text": "..."}
    """

    def __init__(
        self,
        service: GatewayService,
        api_base: str,
        client: httpx.AsyncClient | None = None,
        allow_send: bool = False,
        poll_timeout: int = 25,
    ):
        self.service = service
        self.api_base = api_base.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=40)
        self.allow_send = allow_send
        self.poll_timeout = poll_timeout
        self.polls = 0
        self.events_seen = 0

    async def _call(self, method_path: str, payload: dict | None = None):
        if payload is None:
            resp = await self._client.get(f"{self.api_base}{method_path}")
        else:
            resp = await self._client.post(f"{self.api_base}{method_path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def run_forever(self) -> None:
        while True:
            try:
                updates = await self._call(f"/receive?timeout={self.poll_timeout}")
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(3)
                continue
            self.polls += 1
            await asyncio.sleep(0.05)
            for update in updates or []:
                chat_id = update.get("chat_id", "")
                text = (update.get("text") or "").strip()
                if not chat_id or not text:
                    continue
                self.events_seen += 1
                result = await self.service.handle_message(text)
                reply = result.get("reply") or f"error: {result.get('error', 'unknown')}"
                if self.allow_send:
                    with contextlib.suppress(Exception):
                        await self._call("/send", {"chat_id": chat_id, "text": reply[:4000]})

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()
