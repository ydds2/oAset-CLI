"""oAset as an MCP tool SERVER (stdio, newline-delimited JSON-RPC 2.0).

Lets OTHER agents/editors (Claude Desktop, ZCode, any MCP client) use oAset's
toolset. Read-only tools are exposed by default; write/exec tools opt-in via
    [mcp_server] expose = "all"     # or "readonly" (default)

Run:  oaset mcp-serve [--cwd DIR] [--all-tools]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from oaset import __version__
from oaset.tools import AutoGate, ToolContext, ToolRegistry, default_tools

SERVER_INFO = {"name": "oaset", "version": __version__}
PROTOCOL_VERSION = "2025-06-18"


class McpServerCore:
    """Protocol core: pure message -> response mapping (no I/O), fully testable."""

    def __init__(self, registry: ToolRegistry, expose: str = "readonly", token: str | None = None):
        self.registry = registry
        self.expose = expose  # "readonly" | "all"
        self.token = token
        self.initialized = False
        self._authenticated = False

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method", "")
        request_id = message.get("id")
        if request_id is None:
            return None  # notification
        try:
            result = self._dispatch(method, message.get("params") or {})
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
            }

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.token and not self._authenticated and method not in ("initialize", "ping"):
            raise PermissionError("not authenticated")
        if method == "initialize":
            if self.token:
                sent = (params.get("clientInfo") or {}).get("token", "")
                if sent != self.token:
                    raise PermissionError("invalid token")
                self._authenticated = True
            self.initialized = True
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            }
        if method == "tools/list":
            tools = []
            for tool in self.registry.tools.values():
                if self.expose == "readonly" and tool.permission != "read":
                    continue
                schema = tool.schema()["function"]
                tools.append({
                    "name": schema["name"],
                    "description": schema.get("description", ""),
                    "inputSchema": schema.get("parameters")
                    or {"type": "object", "properties": {}},
                })
            return {"tools": tools}
        if method == "tools/call":
            name = str(params.get("name", ""))
            arguments = params.get("arguments") or {}
            fn = self.registry.tools.get(name)
            if fn is None:
                return {"content": [{"type": "text", "text": f"unknown tool {name}"}],
                        "isError": True}
            registry = self.registry
            if self.expose == "readonly" and fn.permission != "read":
                return {"content": [{"type": "text",
                                     "text": f"tool '{name}' is not exposed (readonly server)"}],
                        "isError": True}

            async def _run():
                return await registry.dispatch(name, json.dumps(arguments))

            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(asyncio.run, _run()).result()
            return {"content": [{"type": "text", "text": result.output}],
                    "isError": result.is_error}
        if method == "ping":
            return {}
        raise KeyError(f"method not found: {method}")


def build_server_registry(cwd: Path, expose: str = "readonly") -> ToolRegistry:
    registry = ToolRegistry(tools=default_tools(), gate=AutoGate())
    registry.bind(ToolContext(cwd=cwd, mode="auto", output_limit=4000))
    if expose == "all":
        return registry
    readonly = ToolRegistry(gate=AutoGate(), tools=[])
    for tool in registry.tools.values():
        if tool.permission == "read":
            readonly.add_tool(tool)
    readonly.bind(registry.ctx)
    return readonly


def serve_stdio(cwd: Path, expose: str = "readonly", token: str | None = None) -> None:
    """Blocking stdio serve loop (newline-delimited JSON-RPC)."""
    registry = build_server_registry(cwd, expose)
    core = McpServerCore(registry, expose, token=token)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = core.handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()

