"""Todo list tool — session-scoped tasks, pinned above the TUI input while live."""

from __future__ import annotations

from oaset.tools.base import READ, Tool, ToolResult

VALID_STATUS = {"pending", "in_progress", "completed"}


class TodoWriteTool(Tool):
    name = "todo_write"
    description = (
        "Maintain the session todo list. Pass the full list of todos with "
        "status pending|in_progress|completed; exactly one should be in_progress while working."
    )
    permission = READ  # session-internal state; no confirmation needed
    required = ["todos"]
    parameters = {
        "todos": {
            "type": "array",
            "description": "Full todo list (replaces previous)",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Task description"},
                    "status": {"type": "string", "description": "pending | in_progress | completed"},
                },
                "required": ["content", "status"],
            },
        }
    }

    async def run(self, args, ctx):
        todos = args.get("todos")
        if not isinstance(todos, list):
            return ToolResult("todos must be an array.", is_error=True)
        cleaned: list[dict[str, str]] = []
        for item in todos:
            if not isinstance(item, dict) or "content" not in item:
                return ToolResult("each todo needs content", is_error=True)
            status = str(item.get("status", "pending"))
            if status not in VALID_STATUS:
                status = "pending"
            cleaned.append({"content": str(item["content"]), "status": status})
        ctx.session_state["todos"] = cleaned
        done = sum(1 for t in cleaned if t["status"] == "completed")
        return ToolResult(f"Todo list updated: {done}/{len(cleaned)} completed")
