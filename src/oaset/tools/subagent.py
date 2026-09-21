"""`task` tool — spawns an isolated sub-agent run and returns its final report."""

from __future__ import annotations

from typing import Any

from oaset.agents import BUILTIN_GENERAL, AgentDef
from oaset.tools.base import READ, Tool, ToolContext, ToolResult
from oaset.utils import one_line, truncate_text


class TaskTool(Tool):
    name = "task"
    description = (
        "Delegate a self-contained task to a sub-agent with its own context window and "
        "tool loop. Use for parallelizable or context-heavy work; report of the "
        "sub-agent comes back as the tool result."
    )
    permission = READ  # inner tool calls go through the same permission gate
    required = ["prompt"]
    parameters = {
        "agent": {"type": "string", "description": "Agent name (see /agents). Default: general"},
        "description": {"type": "string", "description": "One-line task summary for the transcript"},
        "prompt": {"type": "string", "description": "Full task instructions for the sub-agent"},
        "run_in_background": {"type": "boolean", "description": "Run without blocking; the completion report is injected into a later turn. Read with task_output, stop with task_stop."},
    }

    def gate_summary(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return f"task[{args.get('agent', 'general')}]: {one_line(args.get('description', ''), 70)}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        runner = ctx.session_state.get("subagent_runner")
        if runner is None:
            return ToolResult("Sub-agents are not available in this context.", is_error=True)
        agents: dict[str, AgentDef] = ctx.session_state.get("agents", {})
        requested = str(args.get("agent", "general")).strip()
        agent = agents.get(requested.lower())
        if agent is None and requested.lower() == BUILTIN_GENERAL.name:
            agent = BUILTIN_GENERAL  # the built-in fallback always exists
        if agent is None:
            available = ", ".join(sorted(agents)) or "general"
            return ToolResult(
                f"unknown sub-agent {requested!r}; available: {available}", is_error=True)
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return ToolResult("prompt must not be empty.", is_error=True)
        if args.get("run_in_background"):
            from oaset.tools.background import get_registry

            bg = get_registry(ctx)
            description = one_line(args.get("description") or prompt, 60)
            task = bg.start_agent(runner(agent, prompt), f"[{agent.name}] {description}")
            return ToolResult(
                f"Started {task.id} in the background: {description}\n"
                "The completion report is injected into a later turn; "
                "read progress with task_output."
            )
        from oaset.agent.subagent_runner import call_subagent_runner

        meta: dict[str, Any] = {}
        try:
            report = await call_subagent_runner(runner, agent, prompt, meta)
        except Exception as exc:
            return ToolResult(
                f"Sub-agent crashed: {type(exc).__name__}: {exc}"
                + _meta_footer(meta), is_error=True)
        return ToolResult(truncate_text(
            f"[{agent.name}] {report}{_meta_footer(meta)}", ctx.output_limit))


def _meta_footer(meta: dict[str, Any]) -> str:
    """Compact outcome line: status, wall clock, tool calls, tokens."""
    if not meta:
        return ""
    parts: list[str] = []
    status = meta.get("status", "ok")
    if status != "ok":
        parts.append(status.upper())
    elapsed = meta.get("elapsed")
    if elapsed is not None:
        parts.append(f"{elapsed:.1f}s")
    if meta.get("model"):
        parts.append(str(meta["model"]))
    calls = meta.get("tool_calls")
    if calls:
        parts.append(f"{calls} tool calls")
    usage = meta.get("usage") or {}
    if usage.get("total_tokens"):
        parts.append(f"↑{usage.get('prompt_tokens', 0)} ↓{usage.get('completion_tokens', 0)} tok")
    if meta.get("error"):
        parts.append(str(meta["error"]))
    return ("\n\n[" + " · ".join(parts) + "]") if parts else ""
