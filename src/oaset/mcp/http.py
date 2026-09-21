"""MCP HTTP transport (streamable-HTTP, JSON responses) — mcp.json semantics:

{"mcpServers": {"<name>": {"url": "http://host/mcp",
                           "headers": {"X": "y"},
                           "bearerTokenEnvVar": "MY_TOKEN",
                           "enabledTools": [...], "disabledTools": [...],
                           "startupTimeoutMs": 8000}}}

Each request is a JSON-RPC POST accepting either a JSON body or an SSE
`data:` stream; the Mcp-Session-Id response header is captured and replayed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from oaset.mcp.client import (
    CLIENT_INFO,
    DEFAULT_TIMEOUT,
    PROTOCOL_VERSION,
    McpCallResult,
    McpConnection,
    McpError,
    McpToolDef,
)

_SESSION_RE = re.compile(r"^Mcp-Session-Id:\s*(.+)$", re.IGNORECASE)


def parse_sse_data(text: str) -> dict[str, Any] | None:
    """Extract the first JSON payload from an SSE body (or None)."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                continue
    return None


@dataclass
class McpHttpConnection:
    """Stateless streamable-HTTP MCP connection with the stdio client's API surface."""

    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT
    client: httpx.AsyncClient | None = None  # injectable for tests
    network_mode: str = "pull_only"  # local_only blocks HTTP MCP (see git history: ARCHITECTURE §4)

    _session_id: str = ""
    _owns_client: bool = False

    async def start(self) -> None:
        from oaset.network import NetworkBlockedError, NetworkPolicy

        try:
            NetworkPolicy(self.network_mode).assert_allowed("model_call")
        except NetworkBlockedError as exc:
            # wrap so McpManager.start_all can isolate the failure per-server
            raise McpError(f"MCP HTTP server '{self.name}' blocked by network policy: {exc}") from exc
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_client = True
        await self.initialize()

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post(self, message: dict[str, Any], timeout: float | None = None) -> httpx.Response:
        assert self.client is not None
        try:
            resp = await self.client.post(
                self.url,
                content=json.dumps(message),
                headers=self._base_headers(),
                timeout=timeout or self.timeout,
            )
        except httpx.HTTPError as exc:
            raise McpError(f"MCP HTTP server '{self.name}': {exc}") from exc
        if resp.status_code >= 400:
            # a non-2xx at initialize is THIS server's failure, not a batch
            # aborter: without the McpError wrapper it escaped start_all and
            # killed every other server's startup with it
            raise McpError(
                f"MCP HTTP server '{self.name}': HTTP {resp.status_code}")

        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        return resp

    async def initialize(self) -> None:
        resp = await self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            },
            timeout=min(self.timeout, 15.0),
        )
        body = resp.text
        payload: dict[str, Any] | None
        if body.lstrip().startswith("{"):
            payload = json.loads(body)
        else:
            payload = parse_sse_data(body)
        if payload is None:
            raise McpError(f"MCP HTTP server '{self.name}': empty initialize response")
        # stateless servers may answer with a notification-style body
        if payload.get("id") == 1 and "error" in payload:
            raise McpError(f"MCP HTTP server '{self.name}': {payload['error'].get('message', 'error')}")
        await self.notify("notifications/initialized")

    async def notify(self, method: str) -> None:
        assert self.client is not None
        try:
            await self.client.post(
                self.url,
                content=json.dumps({"jsonrpc": "2.0", "method": method}),
                headers=self._base_headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise McpError(f"MCP HTTP server '{self.name}': {exc}") from exc

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        request_id = 100
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        resp = await self._post(message, timeout=timeout)
        text = resp.text
        if text.lstrip().startswith("{"):
            payload = json.loads(text)
        else:
            payload = parse_sse_data(text)
        if payload is None:
            raise McpError(f"MCP HTTP server '{self.name}': malformed response to {method}")
        if "error" in payload:
            raise McpError(f"MCP HTTP server '{self.name}': {payload['error'].get('message', 'error')}")
        return payload.get("result", {})

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
        is_error = bool(result.get("isError"))
        for item in result.get("content", []):
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return McpCallResult(text="\n".join(parts), is_error=is_error)

    async def ping(self) -> bool:
        await self.request("ping", {})
        return True

    async def close(self) -> None:
        if self.client is not None and self._owns_client:
            await self.client.aclose()
        self.client = None


def filter_remote_tools(
    tools: list[McpToolDef],
    enabled: list[str] | None = None,
    disabled: list[str] | None = None,
) -> list[McpToolDef]:
    """mcp.json semantics: enabledTools is a whitelist, disabledTools a blacklist."""
    result = tools
    if enabled:
        allowed = set(enabled)
        result = [t for t in result if t.name in allowed]
    if disabled:
        blocked = set(disabled)
        result = [t for t in result if t.name not in blocked]
    return result


# stdio parity: the manager duck-types both connection classes
McpConnectionLike = McpConnection
