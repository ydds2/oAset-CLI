"""Gateway: one process, many transports.

In-box transport: HTTP (POST /message) — no third-party credentials required.
The big commercial gateways ship transport integrations that need
external bot tokens; oAset exposes the same server/agent seam so additional
transports can be added without touching the agent core.

Local-first posture:
- binds 127.0.0.1 by default and SAYS SO loudly when asked not to;
- POST /message requires a bearer token that never leaves this machine
  (auto-generated into ~/.oaset/gateway_token on first serve);
- one shared asyncio loop and one SessionHost per session id — concurrent
  requests reuse the running MCP manager instead of respawning subprocesses,
  and the conversation persists across requests instead of littering the
  session index with one-session-per-message entries.

Run:  oaset gateway --port 8790 [--model ID] [--mock] [--yolo]
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from oaset.config import AppConfig


def _token_path(home: Path | None = None) -> Path:
    from oaset.utils import oaset_home

    return (home or oaset_home()) / "gateway_token"


def load_or_create_token(home: Path | None = None) -> str:
    """The local auth secret: env override, else a generated file — local-only."""
    import os

    env = os.environ.get("OASET_GATEWAY_TOKEN")
    if env:
        return env
    path = _token_path(home)
    if path.is_file():
        try:
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
        except OSError:
            pass
    token = secrets.token_urlsafe(24)
    from oaset.utils import atomic_write_text

    atomic_write_text(path, token + "\n")
    try:
        import os as _os

        _os.chmod(path, 0o600)
    except OSError:
        pass
    return token


class PanelGate:
    """Permission gate whose approvals come from the local panel.

    Requests publish to every SSE subscriber AND to the pending table;
    POST /panel/approve resolves them. Silence after ``timeout`` denies —
    the same rule every other oAset surface follows.
    """

    def __init__(self, service: "GatewayService", timeout: float = 120.0):
        self.service = service
        self.timeout = timeout
        self._seq = 0
        self._resolved: dict[str, str] = {}
        self._waiters: dict[str, asyncio.Event] = {}
        self._loops: dict[str, asyncio.AbstractEventLoop] = {}

    def deliver(self, rid: str, decision: str) -> None:
        """Called from the HTTP thread: record the panel's decision.

        ``asyncio.Event.set`` is not thread-safe, so the wake goes through the
        awaiting loop; a plain set() from here raced the waiter's wakeup.
        """
        self._resolved[rid] = decision
        waiter = self._waiters.get(rid)
        loop = self._loops.get(rid)
        if waiter is None or loop is None:
            return
        loop.call_soon_threadsafe(waiter.set)

    async def request(self, tool_name: str, level: str, summary: str,
                      preview: str | None = None) -> str:
        import asyncio as _asyncio

        self._seq += 1
        rid = f"approval-{self._seq}"
        entry = {"id": rid, "tool": tool_name, "level": level,
                 "summary": summary, "preview": preview or "",
                 "state": "pending"}
        waiter = _asyncio.Event()
        self._waiters[rid] = waiter
        self._loops[rid] = _asyncio.get_running_loop()
        self.service.pending_approvals[rid] = entry
        try:
            self.service._publish({"type": "approval_requested", **entry})
            await _asyncio.wait_for(waiter.wait(), timeout=self.timeout)
        except _asyncio.TimeoutError:
            entry["state"] = "denied (timeout)"
            return "deny"
        finally:
            self._waiters.pop(rid, None)
            self._loops.pop(rid, None)
            self.service.pending_approvals.pop(rid, None)
        decision = self._resolved.pop(rid, "deny")
        entry["state"] = f"resolved: {decision}"
        return decision if decision in ("allow", "deny") else "deny"


class GatewayService:
    """Async agent surface shared by every transport, one loop, persistent sessions."""

    def __init__(self, cfg: AppConfig, cwd: Path, model_id: str | None = None,
                 yolo: bool = False, provider=None,
                 panel: bool = False, panel_timeout: float = 120.0):
        self.cfg = cfg
        self.cwd = cwd
        self.model_id = model_id
        self.yolo = yolo
        self.provider = provider  # injected (tests); None = build from config
        self.requests_served = 0
        self.loop: asyncio.AbstractEventLoop | None = None
        self._hosts: dict[str, Any] = {}
        # panel approval channel (opt-in via --panel): pending permission
        # requests published to subscribers and resolved over /panel/approve;
        # silence times out to deny — the local-first default
        self.panel = panel
        self.panel_gate = PanelGate(self, timeout=panel_timeout) if panel else None
        self.pending_approvals: dict[str, dict] = {}
        self.approval_timeout: float = 120.0
        # SSE tee (panel/events): canonical events replayed to subscribers
        import collections
        import queue as _queue
        import threading

        self.events: collections.deque = collections.deque(maxlen=500)
        self._subscribers: list[_queue.Queue] = []
        # guards (replay snapshot + registration) against (append + fanout):
        # an event published in that window used to vanish for the new
        # subscriber — rare, but a panel that silently misses an approval
        # request looks exactly like a hung gate
        self._events_lock = threading.Lock()
        self._store: Any = None

    def _store_or_create(self) -> Any:
        if self._store is None:
            from oaset.session import SessionStore

            self._store = SessionStore()
        return self._store

    def subscribe(self) -> "Any":
        import queue as _queue

        # bounded: a subscriber whose /events reader stalls (backgrounded
        # browser tab, slow client) must not grow the process without end.
        # On overflow the OLDEST queued event is dropped — for a live feed
        # the newest matters more — and the drop is counted so the reader
        # can tell the client a gap happened instead of pretending the
        # stream is complete.
        class _SubscriberQueue(_queue.Queue):
            dropped: int = 0

        q: _SubscriberQueue = _SubscriberQueue(maxsize=1000)
        with self._events_lock:
            for past in list(self.events):
                # mark replay WITHOUT re-nesting: past is already the envelope
                q.put_nowait({**past, "replay": True})
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "Any") -> None:
        with self._events_lock:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(q)

    def _offer(self, q: "Any", entry: dict) -> None:
        import queue as _queue

        try:
            q.put_nowait(entry)
        except _queue.Full:
            with contextlib.suppress(Exception):
                q.get_nowait()  # drop oldest, keep the stream current
                q.dropped = int(getattr(q, "dropped", 0)) + 1
                q.put_nowait(entry)

    def _publish(self, event: Any) -> None:
        from oaset.utils import jsonable

        if not hasattr(event, "type"):
            # panel broadcasts (approval_requested) arrive as flat dicts;
            # wrap them into the ONE envelope every SSE consumer parses
            entry = {"type": event.get("type", ""),
                     "session_id": event.get("session_id", ""),
                     "data": {k: v for k, v in event.items()
                              if k not in ("type", "session_id")}}
        else:
            entry = {"type": event.type, "session_id": event.session_id,
                     "data": jsonable(dict(event.data or {}))}
        with self._events_lock:
            self.events.append(entry)
            for q in list(self._subscribers):
                self._offer(q, entry)

    def panel_sessions(self, limit: int = 20) -> list[dict]:
        """Narrow panel API (read-only): the session list, nothing internal."""
        return [
            {"session_id": m.session_id, "cwd": m.cwd, "title": m.title,
             "model": m.model, "updated_at": m.updated_at,
             "message_count": m.message_count}
            for m in self._store_or_create().list_sessions(limit=limit)
        ]

    def panel_usage(self, session_id: str) -> dict:
        store = self._store_or_create()
        session = store.load(session_id)
        if session is None:
            return {"error": "unknown session"}
        totals = store.session_usage(session_id)
        last = store.last_usage(session)
        totals["last"] = {k: last.get(k) for k in
                          ("prompt_tokens", "completion_tokens",
                           "cache_read_tokens", "cache_write_tokens")} if last else None
        return totals

    def panel_evidence(self, session_id: str) -> list[dict]:
        return self._store_or_create().evidence_entries(session_id)

    def _resolve_approval(self, rid: str, decision: str) -> None:
        """Deliver a panel decision to the waiting PanelGate (HTTP thread)."""
        gate = self.panel_gate
        if gate is not None:
            gate.deliver(rid, decision)

    async def _get_host(self, session_id: str | None):
        from oaset.host import SessionHost

        key = session_id or "gateway"
        host = self._hosts.get(key)
        if host is not None:
            return host
        session = None
        store = None
        if session_id and session_id != "new":
            # resume a recorded session when the id matches one on disk
            from oaset.session import SessionStore

            store = SessionStore()
            session = store.load(session_id)
        host = SessionHost(
            self.cfg, self.cwd, model_id=self.model_id, provider=self.provider,
            yolo=self.yolo, persist=True, mcp=True, session=session, store=store,
            # --yolo outranks --panel: the user explicitly opted out of
            # approvals, and a panel gate would silently re-introduce them
            gate=self.panel_gate if (self.panel and not self.yolo) else None,
        )
        self._hosts[key] = host
        return host

    async def handle_message(self, text: str, session_id: str | None = None) -> dict:
        started = time.monotonic()
        try:
            host = await self._get_host(session_id)
            reply = ""
            async for event in host.chat(text):
                self._publish(event)
                if event.type == "turn_completed":
                    reply = str(event.data.get("content", ""))
            self.requests_served += 1
            return {
                "ok": True,
                "reply": reply,
                "session_id": host.session.meta.session_id,
                "elapsed": round(time.monotonic() - started, 2),
            }
        except Exception as exc:  # SessionBusyError lands here with its str
            name = type(exc).__name__
            message = str(exc) or name
            if name == "SessionBusyError":
                message = "busy: a turn is already running for this session"
            return {"ok": False, "error": f"{name}: {message}",
                    "elapsed": round(time.monotonic() - started, 2)}

    async def aclose(self) -> None:
        for host in list(self._hosts.values()):
            try:
                await host.aclose()
            except Exception:
                pass
        self._hosts.clear()


def make_handler(service: GatewayService, token: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # quiet
            pass

        def _authorized(self) -> bool:
            if not token:
                return True
            auth = self.headers.get("authorization") or ""
            if auth.startswith("Bearer ") and auth[7:].strip() == token:
                return True
            return (self.headers.get("x-oaset-token") or "").strip() == token

        def _browser_guard(self) -> str | None:
            """DNS-rebinding / cross-origin defense for this loopback server.

            A browser page can always SEND requests to 127.0.0.1 — what it
            must not do is make them EXECUTE. Two checks close that:
              - Host must be this server's own loopback address (a rebound
                page presents its own domain as Host);
              - a request carrying an Origin (sent by a browser page) must
                name this server's origin; non-browser clients send no Origin
                at all. Combined with the token, form-CSRF cannot drive turns.
            """
            try:
                port = self.server.server_address[1]
            except Exception:
                port = 8790
            allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}",
                             f"[::1]:{port}"}
            host = (self.headers.get("host") or "").lower()
            if host not in allowed_hosts:
                return (f"blocked: Host header {host!r} is not this server's "
                        "loopback address (DNS-rebinding guard)")
            origin = (self.headers.get("origin") or "").strip().lower()
            if origin and origin.rstrip("/") not in {
                    f"http://127.0.0.1:{port}", f"http://localhost:{port}",
                    f"http://[::1]:{port}"}:
                return "blocked: cross-origin request (Origin is not this server)"
            return None

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.rstrip() in ("/health", "/"):
                self._json(200, {"status": "ok", "requests_served": service.requests_served})
                return
            if self.path == "/events":
                self._events_stream()
                return
            if self.path.startswith("/panel/"):
                guard = self._browser_guard()
                if not self._authorized() or guard:
                    self._json(403, {"error": guard or "unauthorized"})
                    return
                from urllib.parse import parse_qs, urlparse

                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                sid = (query.get("session") or [""])[0]
                if parsed.path == "/panel/sessions":
                    self._json(200, {"sessions": service.panel_sessions()})
                elif parsed.path == "/panel/approvals":
                    self._json(200, {"approvals": list(
                        service.pending_approvals.values())})
                elif parsed.path == "/panel/usage" and sid:
                    self._json(200, service.panel_usage(sid))
                elif parsed.path == "/panel/evidence" and sid:
                    self._json(200, {"evidence": service.panel_evidence(sid)})
                else:
                    self._json(404, {"error": "not found"})
                return
            self._json(404, {"error": "not found"})

        def _events_stream(self) -> None:
            """SSE: canonical events of the shared loop, token-gated."""
            guard = self._browser_guard()
            if not self._authorized() or guard:
                self._json(403, {"error": guard or "unauthorized"})
                return
            self.send_response(200)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            q = service.subscribe()
            seen_dropped = 0
            try:
                while True:
                    # a stalled reader that resumed: say WHAT was lost
                    # instead of handing over a silently gapped stream
                    dropped = int(getattr(q, "dropped", 0))
                    if dropped > seen_dropped:
                        seen_dropped = dropped
                        notice = {"type": "events_dropped",
                                  "session_id": "",
                                  "data": {"count": dropped}}
                        self.wfile.write(
                            f"data: {json.dumps(notice, ensure_ascii=False)}\n\n"
                            .encode("utf-8"))
                        self.wfile.flush()
                    try:
                        entry = q.get(timeout=15)
                    except Exception:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    payload = json.dumps(entry, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (ConnectionAbortedError, BrokenPipeError, OSError):
                pass  # client went away: normal for a panel tab closing
            finally:
                service.unsubscribe(q)

        def do_POST(self) -> None:
            if self.path.rstrip() == "/panel/approve":
                guard = self._browser_guard()
                if guard or not self._authorized():
                    self._json(403, {"error": guard or "unauthorized"})
                    return
                try:
                    length = int(self.headers.get("content-length", 0))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    rid = str(payload.get("id", ""))
                    decision = str(payload.get("decision", ""))
                except (ValueError, json.JSONDecodeError):
                    self._json(400, {"error": "invalid JSON body"})
                    return
                if decision not in ("allow", "deny"):
                    self._json(400, {"error": "decision must be allow or deny"})
                    return
                if service.pending_approvals.pop(rid, None) is None:
                    self._json(404, {"error": f"no pending approval {rid!r}"})
                    return
                service._resolve_approval(rid, decision)
                self._json(200, {"ok": True, "id": rid, "decision": decision})
                return
            if self.path.rstrip() != "/message":
                self._json(404, {"error": "not found"})
                return
            if (guard := self._browser_guard()):
                self._json(403, {"error": guard})
                return
            if not self._authorized():
                self._json(401, {"error": "unauthorized: send the local gateway token "
                                       "as `authorization: Bearer <token>` (see ~/.oaset/gateway_token)"})
                return
            try:
                length = int(self.headers.get("content-length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                text = str(payload.get("text", "")).strip()
                if not text:
                    self._json(400, {"error": "text is required"})
                    return
                session_id = str(payload.get("session_id") or "") or None
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"error": "invalid JSON body"})
                return
            # One shared loop: agent/MCP state stays on a single thread, so
            # concurrent requests reuse the running MCP manager instead of
            # each spawning their own subprocess tree.
            coro = service.handle_message(text, session_id)
            if service.loop is not None:
                future = asyncio.run_coroutine_threadsafe(coro, service.loop)
                result = future.result(timeout=1800)
            else:  # transports drive the service on their own loop
                result = asyncio.get_event_loop().run_until_complete(coro)
            self._json(200 if result.get("ok") else 500, result)

    return Handler


def serve(
    cfg: AppConfig,
    cwd: Path,
    host: str = "127.0.0.1",
    port: int = 8790,
    model_id: str | None = None,
    yolo: bool = False,
    provider=None,
    token: str | None = None,
    panel: bool = False,
    panel_timeout: float = 120.0,
) -> ThreadingHTTPServer:
    if token is None:
        token = load_or_create_token()
    service = GatewayService(cfg, cwd, model_id=model_id, yolo=yolo,
                             provider=provider, panel=panel,
                             panel_timeout=panel_timeout)
    server = ThreadingHTTPServer((host, port), make_handler(service, token))
    server.service = service  # type: ignore[attr-defined]

    # The agent loop runs on its own thread; HTTP worker threads only ferry
    # requests onto it via run_coroutine_threadsafe.
    loop = asyncio.new_event_loop()
    service.loop = loop

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    import threading

    thread = threading.Thread(target=_run_loop, daemon=True, name="oaset-gateway-loop")
    thread.start()
    server._loop = loop  # type: ignore[attr-defined]  # for graceful shutdown
    return server


def shutdown_server(server: ThreadingHTTPServer) -> None:
    """Stop HTTP + drain the agent loop (MCP trees included)."""
    try:
        loop = getattr(server, "_loop", None)
        service = getattr(server, "service", None)
        if loop is not None and service is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(service.aclose(), loop).result(timeout=30)
            loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass
    server.server_close()
