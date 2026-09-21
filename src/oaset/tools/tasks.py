"""Background task tools (list / output / stop)."""

from __future__ import annotations

from oaset.tools.background import get_registry
from oaset.tools.base import READ, Tool, ToolResult


class TaskListTool(Tool):
    name = "task_list"
    description = (
        "List background tasks (started with run_shell run_in_background). "
        "Shows id, status and description for each."
    )
    permission = READ
    required = []
    parameters = {
        "active_only": {"type": "boolean", "description": "Only running tasks (default false)"},
        "limit": {"type": "integer", "description": "Max tasks to return (default 20)"},
    }

    async def run(self, args, ctx):
        reg = get_registry(ctx)
        tasks = reg.list(active_only=bool(args.get("active_only")), limit=int(args.get("limit") or 20))
        if not tasks:
            return ToolResult("No background tasks.")
        return ToolResult("\n".join(t.summary() for t in tasks))


class TaskOutputTool(Tool):
    name = "task_output"
    description = (
        "Read the captured output of a background task. Use block=true to wait "
        "for completion (up to the timeout)."
    )
    permission = READ
    required = ["task_id"]
    parameters = {
        "task_id": {"type": "string", "description": "Task id, e.g. task-1"},
        "block": {"type": "boolean", "description": "Wait for completion (default false)"},
        "timeout": {"type": "integer", "description": "Seconds to wait when block=true (default 30)"},
    }

    async def run(self, args, ctx):
        reg = get_registry(ctx)
        text = await reg.output(
            str(args["task_id"]),
            block=bool(args.get("block")),
            timeout=float(args.get("timeout") or 30),
        )
        return ToolResult(text)


class TaskStopTool(Tool):
    name = "task_stop"
    description = "Stop (kill) a running background task."
    permission = READ  # own child process; low risk
    required = ["task_id"]
    parameters = {
        "task_id": {"type": "string", "description": "Task id, e.g. task-1"},
    }

    async def run(self, args, ctx):
        reg = get_registry(ctx)
        try:
            return ToolResult(await reg.stop(str(args["task_id"])))
        except (ProcessLookupError, TimeoutError):
            return ToolResult(f"{args['task_id']} already exited.", is_error=False)
