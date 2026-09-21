"""`parallel_task` tool — run multiple sub-agent tasks concurrently.

Each item gets its own sub-registry and conversation, running via
asyncio.gather. Reports come back in task order.
"""

from __future__ import annotations

import asyncio
from typing import Any

from oaset.agents import BUILTIN_GENERAL, AgentDef
from oaset.tools.base import READ, Tool, ToolContext, ToolResult
from oaset.utils import one_line, truncate_text


class ParallelTaskTool(Tool):
    name = "parallel_task"
    description = (
        "Run multiple sub-agent tasks CONCURRENTLY (对比 task 串行版)。"
        "Each item: {description, prompt, agent?}. Reports return in order. "
        "Use for independent read-only workstreams (research, review, search)."
    )
    permission = READ
    required = ["tasks"]
    parameters = {
        "tasks": {
            "type": "array",
            "description": "2-4 independent tasks to run concurrently",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "prompt": {"type": "string"},
                    "agent": {"type": "string"},
                },
                "required": ["prompt"],
            },
        }
    }

    def gate_summary(self, args, ctx):
        tasks = args.get("tasks", [])
        return f"parallel_task ×{len(tasks)}: " + one_line(
            "; ".join(str(t.get("description", t.get("prompt", ""))) for t in tasks), 90)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        runner = ctx.session_state.get("subagent_runner")
        if runner is None:
            return ToolResult("Sub-agents are not available in this context.", is_error=True)
        tasks = args.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return ToolResult("tasks must be a non-empty array.", is_error=True)
        if len(tasks) > 4:
            return ToolResult("At most 4 parallel tasks.", is_error=True)

        agents: dict[str, AgentDef] = ctx.session_state.get("agents", {})

        async def one(item: dict[str, Any], meta: dict[str, Any]) -> str:
            requested = str(item.get("agent", "general")).strip()
            meta["agent"] = requested
            agent = agents.get(requested.lower())
            if agent is None and requested.lower() == BUILTIN_GENERAL.name:
                agent = BUILTIN_GENERAL
            if agent is None:
                available = ", ".join(sorted(agents)) or "general"
                meta["status"] = "error"
                meta["error"] = f"unknown sub-agent {requested!r} (available: {available})"
                return f"unknown sub-agent {requested!r} (available: {available})"
            prompt = str(item.get("prompt", "")).strip()
            if not prompt:
                meta["status"] = "error"
                meta["error"] = "empty prompt"
                return "(empty prompt)"
            from oaset.agent.subagent_runner import call_subagent_runner

            return await call_subagent_runner(runner, agent, prompt, meta)

        metas: list[dict[str, Any]] = [{} for _ in tasks]
        reports = await asyncio.gather(
            *(one(item, m) for item, m in zip(tasks, metas)), return_exceptions=True)
        parts = []
        for item, report, meta in zip(tasks, reports, metas):
            title = item.get("description") or one_line(str(item.get("prompt", "")), 60)
            if isinstance(report, Exception):
                meta.setdefault("status", "error")
                meta.setdefault("error", f"{type(report).__name__}: {report}")
                parts.append(f"### {title}{_task_header(meta)}\n"
                             f"FAILED: {type(report).__name__}: {report}")
            else:
                parts.append(f"### {title}{_task_header(meta)}\n{report}")
        combined = "\n\n---\n\n".join(parts)
        return ToolResult(truncate_text(combined, ctx.output_limit))


def _task_header(meta: dict[str, Any]) -> str:
    """One structured line per task: agent · status · elapsed · tools · tokens."""
    if not meta:
        return ""
    bits: list[str] = []
    if meta.get("agent"):
        bits.append(str(meta["agent"]))
    status = meta.get("status", "ok")
    if status != "ok":
        bits.append(status.upper())
    if meta.get("elapsed") is not None:
        bits.append(f"{meta['elapsed']:.1f}s")
    if meta.get("tool_calls"):
        bits.append(f"{meta['tool_calls']} tool calls")
    usage = meta.get("usage") or {}
    if usage.get("total_tokens"):
        bits.append(f"\u2191{usage.get('prompt_tokens', 0)} "
                    f"\u2193{usage.get('completion_tokens', 0)} tok")
    return ("\n`" + " · ".join(bits) + "`") if bits else ""
