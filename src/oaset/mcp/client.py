"""MCP (Model Context Protocol) stdio client — JSON-RPC 2.0 over newline-delimited JSON.

Talks to local MCP servers the same way Kimi Code's mcp.json declares them:
{"mcpServers": {"<name>": {"command": "...", "args": [...], "env": {...}}}}
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "oaset", "version": "0.1.0"}
DEFAULT_TIMEOUT = 30.0

# Ambient environment variables MCP server subprocesses may inherit by default.
# Deliberately small: MCP servers are third-party code declared in config, so
# ambient secrets (API keys, tokens, passwords) must never leak into them
# implicitly — credentials reach a server only when the user names them in the
# server's `env` mapping, or opts in via `inherit_env: true`.
MCP_ENV_ALLOWLIST = frozenset({
    "PATH", "PATHEXT",
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "TEMP", "TMP", "TMPDIR",
    "LANG", "LC_ALL", "TERM",
    "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "WINDIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "NO_PROXY",
})


def build_mcp_env(
    declared: dict[str, str] | None = None, *, inherit_env: bool = False
) -> dict[str, str]:
    """Environment for an MCP server subprocess.

    Default: the non-secret allowlist above + the server's explicitly
    declared `env`. `inherit_env=True` is the explicit escape hatch that
    passes the full parent environment through (surfaced as a warning in the
    TUI when configured).
    """
    if inherit_env:
        return {**os.environ, **(declared or {})}
    env = {k: v for k, v in os.environ.items() if k.upper() in MCP_ENV_ALLOWLIST}
    env.update(declared or {})
    return env


# Package-runner shims that ship as .cmd/.bat on Windows: CreateProcess cannot
# exec them directly, so stdio spawns route through `cmd /c` (see start()).
_CMD_SHIMS = frozenset({"npx", "npm", "yarn", "pnpm", "pnpx", "uvx", "uv"})


def _needs_cmd_shim(command: str) -> bool:
    """True for commands Windows must launch through cmd (batch shims)."""
    name = os.path.basename(command.strip('"')).lower()
    stem = name.removesuffix(".cmd").removesuffix(".bat").removesuffix(".exe")
    return name.endswith((".cmd", ".bat")) or stem in _CMD_SHIMS


class McpError(Exception):
    pass


@dataclass
class McpToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class McpCallResult:
    text: str
    is_error: bool = False


@dataclass
class McpConnection:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT
    inherit_env: bool = False

    _proc: asyncio.subprocess.Process | None = None
    _pending: dict[int, asyncio.Future] = field(default_factory=dict)
    _stderr_tail: deque = field(default_factory=lambda: deque(maxlen=20))
    _ids = itertools.count(1)
    _reader_task: asyncio.Task | None = None
    _handler_tasks: set = field(default_factory=set)  # in-flight server requests
    # async (params) -> MCP sampling result dict | None (None = denied).
    # None (default) keeps sampling refused with -32601.
    sampling_handler: Any = None
    # async (params) -> elicitation result {"action": "accept"/"decline"/
    # "cancel", "content": {...}} | None (None = denied). Injected by the TUI
    # with an approval-gated, per-field inline form.
    elicitation_handler: Any = None
    # async (params) -> {"roots": [{"uri": ..., "name": ...}]} | None.
    # Injected by the TUI to expose the workspace root (gated).
    roots_handler: Any = None

    async def start(self) -> None:
        env = build_mcp_env(self.env, inherit_env=self.inherit_env)
        command, args = self.command, self.args
        if sys.platform == "win32" and _needs_cmd_shim(command):
            # Windows CreateProcess cannot launch .cmd/.bat shims (npx, npm…)
            # directly: exec fails with FileNotFoundError. Route through cmd.
            command, args = "cmd", ["/c", command, *args]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise McpError(f"cannot start MCP server '{self.name}': {exc}") from exc
        self._reader_task = asyncio.create_task(self._read_loop())
        asyncio.create_task(self._drain_stderr())
        try:
            await self.initialize()
        except Exception:
            # a wedged/timed-out initialize used to leak BOTH the live
            # subprocess and its reader/drain tasks — one hanging npx server
            # per failed start, accumulating per session host
            await self.close()
            raise

    async def initialize(self) -> None:
        await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
            timeout=min(self.timeout, 15.0),
        )
        self.notify("notifications/initialized")

    # ------------------------------------------------------------------ core

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        from oaset.mcp.methods import classify_message

        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = classify_message(message)
            if kind == "notification":
                # notifications are accepted and dropped — never answered
                continue
            if kind == "request":
                method = message.get("method", "")
                handler = {
                    "sampling/createMessage": self.sampling_handler,
                    "elicitation/create": self.elicitation_handler,
                    "roots/list": self.roots_handler,
                }.get(method)
                if handler is not None:
                    # serve asynchronously so one slow handler cannot stall
                    # unrelated frames (responses/notifications) on this conn
                    task = asyncio.ensure_future(self._serve_handler(message, handler))
                    self._handler_tasks.add(task)
                    task.add_done_callback(self._handler_tasks.discard)
                    continue
                # other server→client requests: protocol requires a response;
                # unsupported methods get -32601 instead of leaving the server
                # awaiting forever
                self._write_json({
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {
                        "code": -32601,
                        "message": f"oaset does not support server method "
                                   f"{message.get('method', '?')!r}",
                    },
                })
                continue
            if kind != "response":
                continue
            request_id = message.get("id")
            if request_id is not None and request_id in self._pending:
                future = self._pending.pop(request_id)
                if not future.done():
                    if "error" in message:
                        err = message["error"]
                        future.set_exception(
                            McpError(f"{self.name}: {err.get('message', err)}")
                        )
                    else:
                        future.set_result(message.get("result", {}))

        # stdin EOF: drain in-flight handler tasks so their responses are
        # actually written before the loop returns (servers often close
        # stdin right after the last request — e.g. batch-style probes)
        if self._handler_tasks:
            await asyncio.gather(*set(self._handler_tasks), return_exceptions=True)

    def _write_json(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))

    async def _serve_sampling(self, message: dict[str, Any]) -> None:
        """Back-compat wrapper: serve one sampling/createMessage request."""
        await self._serve_handler(message, self.sampling_handler)

    async def _serve_handler(self, message: dict[str, Any], handler: Any) -> None:
        """Answer one server→client request via an injected handler.

        handler result dict → success response; None → denied (-32002);
        exception → internal error (-32603). Never silently drops the request.
        """
        request_id = message.get("id")
        try:
            result = await handler(message.get("params") or {})
        except Exception as exc:
            self._write_json({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32603,
                "message": f"handler failed: {type(exc).__name__}: {exc}",
            }})
            return
        if result is None:
            self._write_json({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32002,
                "message": "request denied by user/policy",
            }})
            return
        self._write_json({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            self._stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))

    async def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        if self._proc is None or self._proc.stdin is None:
            raise McpError(f"MCP server '{self.name}' is not running")
        request_id = next(self._ids)
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        assert self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        try:
            return await asyncio.wait_for(future, timeout=timeout or self.timeout)
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise McpError(f"MCP server '{self.name}' timed out on {method}") from None
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise

    # ------------------------------------------------------------------ API

    async def list_tools(self) -> list[McpToolDef]:
        result = await self.request("tools/list", {})
        tools: list[McpToolDef] = []
        for item in result.get("tools", []):
            tools.append(
                McpToolDef(
                    name=str(item.get("name", "")),
                    description=str(item.get("description", "")),
                    input_schema=item.get("inputSchema") or {"type": "object", "properties": {}},
                )
            )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        result = await self.request("tools/call", {"name": name, "arguments": arguments})
        parts: list[str] = []
        for piece in result.get("content", []) or []:
            if piece.get("type") == "text":
                parts.append(str(piece.get("text", "")))
            elif piece.get("type") == "resource":
                resource = piece.get("resource", {})
                parts.append(str(resource.get("text", "(binary resource)")))
            else:
                parts.append(f"({piece.get('type', 'unknown')} content)")
        return McpCallResult(
            text="\n".join(parts) if parts else "(empty response)",
            is_error=bool(result.get("isError", False)),
        )

    async def ping(self) -> bool:
        try:
            await self.request("ping", {}, timeout=5)
            return True
        except McpError:
            return False

    async def list_resources(self) -> list[dict[str, Any]]:
        """Read-only resources/list; servers without the capability return []."""
        try:
            result = await self.request("resources/list", {})
        except McpError as exc:
            if "method not found" in str(exc).lower() or "-32601" in str(exc):
                return []
            raise
        return list(result.get("resources", []) or [])

    async def read_resource(self, uri: str) -> dict[str, Any]:
        return await self.request("resources/read", {"uri": uri})

    async def list_prompts(self) -> list[dict[str, Any]]:
        """Read-only prompts/list; servers without the capability return []."""
        try:
            result = await self.request("prompts/list", {})
        except McpError as exc:
            if "method not found" in str(exc).lower() or "-32601" in str(exc):
                return []
            raise
        return list(result.get("prompts", []) or [])

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await self.request("prompts/get", {"name": name, "arguments": arguments or {}})

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (TimeoutError, ProcessLookupError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            self._proc = None

    def stderr_summary(self) -> str:
        return " | ".join(self._stderr_tail)[-400:] if self._stderr_tail else ""


def sanitize_tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", f"mcp__{server}__{tool}")
