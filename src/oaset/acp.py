"""ACP-lite: newline-delimited JSON-RPC 2.0 on stdio so editors can drive oAset.

This is not the full agent-client-protocol SDK; it speaks the same shape
for the methods we need:

    initialize, session/new, session/load, session/list, session/prompt,
    session/cancel, ping            (client → server)
    session/update                  (server → client notification, live)
    session/request_permission      (server → client REQUEST; the desktop
                                     permission popup — the client answers
                                     allow_once / allow_always / reject)

Local-first notes: everything runs on stdio between two local processes —
no network, no relay. The gate default on client silence is *deny*.

The serve loop is a single asyncio event loop: a reader thread feeds stdin
lines into a queue, a writer task flushes ``session/update`` the moment an
event lands (real-time progress), and ``session/prompt`` runs as a task so
``session/cancel`` (a NOTIFICATION — no id) is processed while it runs.
Cancelling the prompt task abandons the kernel's async generator, which is
the kernel's own hard-cancel path (partial output is preserved).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
from pathlib import Path
from typing import Any, Iterator

from oaset import __version__
from oaset.config import AppConfig, load_config
from oaset.errors import SessionBusyError
from oaset.host import SessionHost
from oaset.tools import AutoGate
from oaset.utils import jsonable

ACP_APPROVAL_TIMEOUT = 300.0  # seconds before an unanswered editor approval denies

PROTOCOL_VERSION = 1
AGENT_INFO = {"name": "oaset", "version": __version__}


class MethodNotFound(Exception):
    """The requested JSON-RPC method does not exist (-32601).

    Split from KeyError so 'no such method' and 'no such session' stop
    sharing an error code — clients could not branch on them."""

# Server-initiated request ids start far above any client id space.
_SERVER_REQ_BASE = 1_000_000_000

ALLOWED_DECISIONS = ("allow", "always", "deny")


def _prompt_parts(prompt: Any) -> tuple[str, list[dict]]:
    """Split an ACP prompt into (text, image parts).

    Image blocks arrive as ``{"type":"image","data":"<b64>","mimeType":...}``;
    the kernel consumes OpenAI-style ``image_url`` data-URI parts, so the
    conversion happens here — claiming image support and honouring it.
    """
    if isinstance(prompt, str):
        return prompt, []
    import base64

    texts: list[str] = []
    images: list[dict] = []
    blocks = prompt if isinstance(prompt, list) else [prompt]
    for block in blocks:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict):
            if block.get("type") == "image" and block.get("data"):
                data = block["data"]
                mime = str(block.get("mimeType") or "image/png")
                # contract enforcement at the door: garbage used to pass
                # through and detonate later as an unrelated provider error
                if not isinstance(data, str):
                    raise KeyError("invalid image block: data must be a base64 string")
                if not mime.startswith("image/") or any(c in mime for c in "\r\n ;"):
                    raise KeyError(f"invalid image block: bad mimeType {mime!r}")
                try:
                    base64.b64decode(data, validate=True)
                except Exception:
                    raise KeyError("invalid image block: data is not valid base64") from None
                images.append({"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64,{data}"}})
            elif block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
            elif block.get("text"):
                texts.append(str(block["text"]))
    return "".join(texts), images


class AcpGate:
    """PermissionGate over the wire: ask the client, await the answer.

    Local-first default is deny — a client that never answers (or died)
    must not silently grant filesystem writes. Session cancel also denies
    every pending approval: an interrupted turn must not keep prompting.
    """

    def __init__(self, server: "AcpServer", session_id: str) -> None:
        self._server = server
        self._sid = session_id

    async def request(self, tool_name: str, level: str, summary: str,
                      preview: str | None = None) -> str:
        import asyncio as _asyncio

        # an alive-but-silent client used to hold the turn open forever; the
        # documented "silence is deny" needs an actual clock
        try:
            raw = await _asyncio.wait_for(
                self._server.request_permission(
                    self._sid, tool_name=tool_name, level=level,
                    summary=summary, preview=preview),
                timeout=ACP_APPROVAL_TIMEOUT)
        except _asyncio.TimeoutError:
            return "deny"
        decision = str(raw.get("decision") or raw.get("outcome") or "")
        option_id = str(raw.get("optionId") or raw.get("option_id") or "")
        if decision not in ALLOWED_DECISIONS:
            decision = {"allow_once": "allow", "allow_always": "always",
                        "reject_once": "deny", "reject": "deny"}.get(option_id, "")
        return decision if decision in ALLOWED_DECISIONS else "deny"


class AcpServer:
    """Async request handling; all wire I/O lives in serve_stdio()."""

    def __init__(self, cfg: AppConfig | None = None, cwd: Path | None = None,
                 yolo: bool = False):
        self.cfg = cfg or load_config()
        self.cwd = Path(cwd or Path.cwd())
        self.yolo = yolo
        self.initialized = False
        self.sessions: dict[str, SessionHost] = {}
        self.updates: list[dict[str, Any]] = []   # only used by sync probes/tests
        self.outbound: asyncio.Queue | None = None
        self._req_counter = _SERVER_REQ_BASE
        self._pending_approvals: dict[int, asyncio.Future] = {}
        self._approval_sessions: dict[int, str] = {}
        self._prompt_tasks: dict[str, asyncio.Task] = {}
        # a cancel that raced ahead of its prompt task's first step
        self._cancel_requested: set[str] = set()

    # ------------------------------------------------------------ plumbing

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.outbound is not None:
            self.outbound.put_nowait(payload)
        else:  # no loop attached (sync probe): park it for inspection
            self.updates.append(payload)

    def _next_req_id(self) -> int:
        self._req_counter += 1
        return self._req_counter

    # ------------------------------------------------------ request routing

    async def handle_async(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Route one inbound message. Client RESPONSES (no method) resolve
        pending server requests; notifications run even without an id."""
        method = message.get("method")
        request_id = message.get("id")
        if method is None:
            self._resolve_client_response(message)
            return None
        if str(method) in ("session/prompt", "prompt") and request_id is None:
            # a NOTIFICATION prompt would run the whole turn inline on the
            # read loop — exactly the death the task path below exists to
            # prevent; only a non-conforming client ever sends one
            self._emit({"jsonrpc": "2.0", "method": "session/update",
                        "params": {"update": {
                            "sessionUpdate": "error",
                            "data": {"message":
                                     "session/prompt requires a request id"}}}})
            return None
        if str(method) in ("session/prompt", "prompt") and request_id is not None:
            # MUST NOT be awaited here: awaiting the turn blocks the read
            # loop, so a session/cancel notification was only ever read AFTER
            # the turn finished (the tests used concurrent handle_async calls
            # and never saw the shipped loop). Run it as a task; the response
            # is emitted when it completes.
            asyncio.get_running_loop().create_task(
                self._prompt_and_respond(request_id, message.get("params") or {}))
            return None
        try:
            result = await self._dispatch(str(method), message.get("params") or {})
            if request_id is None:
                return None  # notification: no response
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except MethodNotFound as exc:
            if request_id is None:
                return None
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": str(exc)}}
        except KeyError as exc:
            # unknown session / bad params: -32602 (invalid params), NOT
            # method-not-found — the two used to share -32601, so a client
            # could not branch on "no such session" vs "no such method"
            if request_id is None:
                return None
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602,
                              "message": exc.args[0] if exc.args else str(exc)}}
        except Exception as exc:
            if request_id is None:
                return None
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}

    async def _prompt_and_respond(self, request_id: Any, params: dict[str, Any]) -> None:
        try:
            result = await self._prompt(params)
            self._emit({"jsonrpc": "2.0", "id": request_id, "result": result})
        except KeyError as exc:  # unknown session: invalid params, not "no method"
            self._emit({"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32602,
                                  "message": exc.args[0] if exc.args else str(exc)}})
        except SessionBusyError as exc:
            # the SAME structured shape gateway/SDK use, so clients branch
            # once — not once per surface
            self._emit({"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32000, "message": str(exc),
                                  "data": {"code": "session_busy"}}})
        except Exception as exc:
            self._emit({"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}})

    def _resolve_client_response(self, message: dict[str, Any]) -> None:
        raw_id = message.get("id")
        try:
            req_id = int(raw_id) if raw_id is not None else -1
        except (TypeError, ValueError):
            return
        if req_id < 0:
            return  # no usable id: nothing to resolve
        future = self._pending_approvals.pop(req_id, None)
        if future is not None and not future.done():
            future.set_result(message.get("result") if "result" in message else {})

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            self.initialized = True
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {
                    "loadSession": True,
                    "promptCapabilities": {"image": True, "audio": False},
                },
                "agentInfo": AGENT_INFO,
            }
        if method == "ping":
            return {}
        if method in ("session/new", "newSession"):
            return self._new_session(params)
        if method in ("session/load", "loadSession"):
            return self._load_session(params)
        if method in ("session/list", "listSessions"):
            return self._list_sessions(params)
        if method in ("session/prompt", "prompt"):
            return await self._prompt(params)  # direct callers (tests) only
        if method in ("session/cancel", "cancel"):
            self._cancel(str(params.get("sessionId") or params.get("session_id") or ""))
            return {}
        raise MethodNotFound(f"method not found: {method}")

    # ------------------------------------------------------------- sessions

    def _build_host(self, cwd: Path, session=None, store=None, mcp: bool = True) -> SessionHost:
        return SessionHost(
            self.cfg, cwd, yolo=self.yolo,
            gate=AutoGate() if self.yolo else None,
            persist=True, mcp=mcp, session=session, store=store,
        )

    def _new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = Path(params.get("cwd") or self.cwd)
        host = self._build_host(cwd, mcp=bool(params.get("mcp", True)))
        sid = host.session.meta.session_id
        self._wire_gate(host, sid)
        self.sessions[sid] = host
        return {"sessionId": sid}

    def _load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("sessionId") or params.get("session_id") or "")
        if sid in self.sessions:
            return {"sessionId": sid}
        from oaset.session import SessionStore

        cwd = Path(params.get("cwd") or self.cwd)
        store = SessionStore()
        session = store.load(sid)
        if session is None:
            raise KeyError(f"unknown session {sid}")
        host = self._build_host(cwd, session=session, store=store,
                                mcp=bool(params.get("mcp", True)))
        self._wire_gate(host, sid)
        self.sessions[sid] = host
        return {"sessionId": sid}

    def _wire_gate(self, host: SessionHost, sid: str) -> None:
        """Route permission asks to the client over the wire.

        The gate lives on the registry (and is copied into the context at
        bind time), so both spots must point at the ACP gate. In --yolo the
        AutoGate was already installed at construction; never downgrade it.
        """
        if self.yolo:
            return
        host.set_gate(AcpGate(self, sid))

    def _list_sessions(self, params: dict[str, Any]) -> dict[str, Any]:
        from oaset.session import SessionStore

        cwd = str(params.get("cwd") or self.cwd)
        metas = SessionStore().list_sessions(cwd=cwd, limit=int(params.get("limit") or 20))
        return {
            "sessions": [
                {
                    "sessionId": m.session_id,
                    "cwd": m.cwd,
                    "title": m.title,
                    "updatedAt": m.updated_at,
                }
                for m in metas
            ]
        }

    # -------------------------------------------------------------- prompts

    async def aclose(self) -> None:
        """Close every live SessionHost (EOF / shutdown path)."""
        for host in list(self.sessions.values()):
            with contextlib.suppress(Exception):
                await host.aclose()
        self.sessions.clear()

    def _cancel(self, sid: str) -> None:
        # latch FIRST: a cancel buffered behind its own prompt arrives
        # before the prompt task has run a step and registered itself —
        # the map lookup alone was a no-op and the turn ran to completion
        self._cancel_requested.add(sid)
        task = self._prompt_tasks.get(sid)
        if task is not None and not task.done():
            task.cancel()
        # An interrupted turn must not leave the client holding a permission
        # popup: deny every pending approval belonging to this session.
        for req_id, future in list(self._pending_approvals.items()):
            if not future.done() and self._approval_sessions.get(req_id) == sid:
                future.set_result({})
                self._pending_approvals.pop(req_id, None)

    async def request_permission(self, session_id: str, *, tool_name: str, level: str,
                                 summary: str, preview: str | None) -> dict[str, Any]:
        req_id = self._next_req_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_approvals[req_id] = future
        self._approval_sessions[req_id] = session_id
        self._emit({
            "jsonrpc": "2.0", "id": req_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {"toolName": tool_name, "level": level,
                             "summary": summary, "preview": preview or ""},
                "options": [
                    {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
                    {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                ],
            },
        })
        try:
            return await future
        finally:
            self._pending_approvals.pop(req_id, None)
            self._approval_sessions.pop(req_id, None)

    def _emit_update(self, session_id: str, event: Any) -> None:
        self._emit({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": event.type,
                    "data": _jsonable(dict(event.data)),
                },
            },
        })

    async def _prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("sessionId") or params.get("session_id") or "")
        host = self.sessions.get(sid)
        if host is None:
            raise KeyError(f"unknown session {sid}")
        if sid in self._prompt_tasks and not self._prompt_tasks[sid].done():
            raise SessionBusyError()
        text, images = _prompt_parts(params.get("prompt") or params.get("text") or "")

        # The turn runs as its own task so cancel can land mid-stream; the
        # response below resolves when the task finishes (stopReason set).
        done = asyncio.get_running_loop().create_future()

        async def runner() -> None:
            answer = ""
            stop_reason = "end_turn"
            try:
                # consume to NATURAL end, not break-at-turn_completed: the
                # loop can still emit budget_exhausted after turn_done, and
                # an abandoned generator left _turn_active set until GC ran
                # its finalizer. The response resolves at turn_completed;
                # the stream keeps draining until the kernel closes it.
                _stream = host.chat(text, images=images or None)
                # chat() is an async generator — aclosing closes it
                # deterministically even when mypy can't see the aclose
                async with contextlib.aclosing(_stream) as stream:  # type: ignore[type-var]
                    async for event in stream:
                        self._emit_update(sid, event)
                        if event.type == "assistant_delta":
                            answer += str(event.data.get("text") or "")
                        elif event.type == "turn_completed":
                            content = event.data.get("content")
                            answer = content if content is not None else answer
                            if not done.done():
                                done.set_result({"stopReason": stop_reason,
                                                 "text": answer})
            except asyncio.CancelledError:
                stop_reason = "cancelled"
            except Exception as exc:
                stop_reason = "error"
                self._emit({"jsonrpc": "2.0", "method": "session/update",
                            "params": {"sessionId": sid, "update": {
                                "sessionUpdate": "error",
                                "data": {"message": f"{type(exc).__name__}: {exc}"}}}})
            finally:
                self._prompt_tasks.pop(sid, None)
                self._cancel_requested.discard(sid)
                if not done.done():
                    done.set_result({"stopReason": stop_reason, "text": answer})

        task = asyncio.get_running_loop().create_task(runner())
        self._prompt_tasks[sid] = task
        # consume a cancel that landed between task creation and the
        # runner's first step (the pipelined prompt+cancel race)
        if sid in self._cancel_requested:
            self._cancel_requested.discard(sid)
            task.cancel()
        return await done


def _jsonable(value: Any, _depth: int = 0) -> Any:
    return jsonable(value, _depth)


def _write_message(payload: dict[str, Any], *, lsp: bool) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=_jsonable)
    if lsp:
        encoded = body.encode("utf-8")
        # BYTES, not text: TextIOWrapper translates \n to os.linesep on
        # Windows, so the text path emitted "\r\r\n" headers — a framing
        # violation every strict LSP client rejects. The read side already
        # consumes sys.stdin.buffer; the write side must be symmetric.
        out = getattr(sys.stdout, "buffer", None)
        if out is not None:
            out.write(("Content-Length: %d\r\n\r\n" % len(encoded)).encode("ascii")
                      + encoded)
            out.flush()
        else:  # pragma: no cover - stdout replaced by a text-only object
            sys.stdout.write("Content-Length: %d\r\n\r\n" % len(encoded))
            sys.stdout.write(body)
            sys.stdout.flush()
    else:
        sys.stdout.write(body + "\n")
        sys.stdout.flush()


def _iter_lsp_frames(stream) -> Iterator[str]:
    """Yield JSON bodies from a ``Content-Length`` framed byte stream.

    LSP framing is length-prefixed and puts NOTHING after the body — the next
    frame's header starts immediately. A line reader therefore glued a body to
    the following header and dropped both, which made ``acp --lsp`` unable to
    receive a second request; reading by byte count is the only correct way.
    """
    while True:
        length = -1
        while True:
            header = stream.readline()
            if not header:            # EOF
                return
            if header in (b"\r\n", b"\n"):
                break
            name, _, value = header.decode("ascii", "replace").partition(":")
            if name.strip().lower() == "content-length":
                try:
                    length = int(value.strip())
                except ValueError:
                    length = -1
        if length <= 0:
            continue
        body = stream.read(length)
        if not body:
            return
        yield body.decode("utf-8", "replace")


def _read_stdin(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue,
                *, lsp: bool = False) -> None:
    """Reader thread: blocking stdin → thread-safe queue (EOF as None)."""
    try:
        if lsp:
            buffer = getattr(sys.stdin, "buffer", None) or sys.stdin
            for body in _iter_lsp_frames(buffer):
                loop.call_soon_threadsafe(queue.put_nowait, body)
        else:
            for raw in sys.stdin:
                loop.call_soon_threadsafe(queue.put_nowait, raw)
    except Exception:
        pass
    finally:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, None)
        except RuntimeError:
            pass  # the loop closed before stdin EOF (tests, abrupt exit)


async def _writer(outbound: asyncio.Queue, lsp: bool) -> None:
    loop = asyncio.get_running_loop()
    while True:
        payload = await outbound.get()
        try:
            # stdout is BLOCKING I/O: a live client that stops reading fills
            # the OS pipe, and a write on the event-loop thread then froze
            # the whole server — cancel, approvals, everything. The default
            # executor keeps the loop responsive; the single writer task
            # preserves frame order.
            import functools

            await loop.run_in_executor(
                None, functools.partial(_write_message, payload, lsp=lsp))
        except Exception as exc:
            # one unserialisable frame must not silence the session: report it
            # out of band and keep serving the frames after it
            print(f"oaset acp: dropped an unserialisable frame ({type(exc).__name__}: {exc})",
                  file=sys.stderr, flush=True)


async def _serve(server: AcpServer, lsp: bool) -> None:
    loop = asyncio.get_running_loop()
    inbound: asyncio.Queue = asyncio.Queue()
    server.outbound = asyncio.Queue()
    writer = asyncio.create_task(_writer(server.outbound, lsp))
    threading.Thread(target=_read_stdin, args=(loop, inbound),
                     kwargs={"lsp": lsp}, daemon=True).start()

    # Local cleanup backstop: MCP servers and shells die with this process.
    try:
        from oaset.sandbox import attach_current_process_to_cleanup_job

        attach_current_process_to_cleanup_job()
    except Exception:
        pass
    _install_signal_graceful_shutdown()

    try:
        while True:
            raw = await inbound.get()
            if raw is None:  # EOF: the client went away
                break
            line = raw.strip()
            if not line:
                continue
            if line.lower().startswith("content-length:"):
                continue  # LSP header; the JSON body is the next line
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            response = await server.handle_async(message)
            if response is not None:
                server._emit(response)
    finally:
        for task in list(server._prompt_tasks.values()):
            if not task.done():
                task.cancel()
        await asyncio.sleep(0)  # let cancellations land
        for future in list(server._pending_approvals.values()):
            if not future.done():
                future.set_result({})   # deny on shutdown: local-first default
        await server.aclose()
        writer.cancel()


def _install_signal_graceful_shutdown() -> None:
    """SIGINT/SIGTERM → clean exit instead of an orphaned MCP tree."""
    import signal

    def _bye(signum, frame) -> None:
        raise SystemExit(0)

    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        handler = getattr(signal, sig, None)
        if handler is not None:
            try:
                signal.signal(handler, _bye)
            except (ValueError, OSError):
                pass


def serve_stdio(cwd: Path | None = None, yolo: bool = False, *, lsp: bool = False) -> None:
    """Blocking stdio serve loop (newline JSON-RPC, or LSP Content-Length frames)."""
    server = AcpServer(cwd=cwd, yolo=yolo)
    try:
        asyncio.run(_serve(server, lsp))
    except (KeyboardInterrupt, SystemExit):
        pass
