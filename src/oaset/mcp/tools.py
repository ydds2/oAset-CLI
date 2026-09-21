"""Tool adapter exposing a remote MCP tool to oAset's ToolRegistry."""

from __future__ import annotations

import json
from typing import Any

from oaset.mcp.client import McpCallResult, McpConnection, McpToolDef
from oaset.tools.base import EXEC, Tool, ToolContext, ToolResult
from oaset.utils import one_line, truncate_text


class McpToolAdapter(Tool):
    """Remote MCP tool surfaced locally. EXEC-level: remote code runs on every call."""

    def __init__(self, server: str, conn: McpConnection, tool: McpToolDef):
        from oaset.mcp.client import sanitize_tool_name

        self.server = server
        self.conn = conn
        self.tool = tool
        self.remote_name = tool.name
        self.name = sanitize_tool_name(server, tool.name)
        self.description = f"[MCP:{server}] {tool.description}".strip()
        self.permission = EXEC
        self.input_schema = tool.input_schema

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def gate_summary(self, args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            return f"mcp {self.server}.{self.remote_name} {one_line(json.dumps(args, ensure_ascii=False), 90)}"
        except (TypeError, ValueError):
            return f"mcp {self.server}.{self.remote_name}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result: McpCallResult = await self.conn.call_tool(self.remote_name, args)
        except Exception as exc:
            return ToolResult(f"MCP call failed: {exc}", is_error=True)
        return ToolResult(truncate_text(result.text, ctx.output_limit), is_error=result.is_error)
