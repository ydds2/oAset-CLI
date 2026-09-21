"""Factory for the sub-agent runner used by the `task` tool (app and one-shot CLI)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from typing import Any

from oaset.agent.messages import Conversation
from oaset.agent.prompts import build_system_prompt
from oaset.agents import SPAWN_DEPTH_LIMIT
from oaset.kernel import AgentKernel
from oaset.tools import ToolContext, ToolRegistry

# tools a sub-agent must never inherit (no fan-out from inside a sub-agent)
NON_INHERITED_TOOLS = ("task", "parallel_task")


# The declared progress vocabulary of on_event(agent, kind, payload).
# Consumers (TUI live card, panels, tests) may rely on exactly these kinds:
#   start      {prompt}            — task began (prompt clamped to 200 chars)
#   tool_start {name}              — the sub-agent called a tool
#   tool_end   {name, is_error}    — the call finished
#   delta      {text}              — streaming text (clamped to 120 chars)
#   timeout    {}                  — wall-clock limit hit
#   error      {error}             — the task raised
#   done       {}                  — final answer follows in the report
SUBAGENT_EVENT_KINDS = ("start", "tool_start", "tool_end", "delta",
                        "timeout", "error", "done")


def make_subagent_runner(
    provider: Any,
    base_registry: ToolRegistry,
    base_ctx: ToolContext,
    max_iterations: int = 0,
    context_max_tokens: int | None = None,
    *,
    depth: int = 0,
    on_event: Any = None,
    config: Any = None,
    budget_tokens: int | Any = 0,  # int or zero-arg callable (live budget)
):
    """Build `async run(agent, prompt) -> report`.

    The sub-agent gets a fresh conversation (base system prompt + the agent's
    own role prompt) and a registry filtered by the agent's tool whitelist.
    `task`/`parallel_task` are removed so sub-agents cannot fan out further.

    Per-agent options honoured: `agent.model` (built through the shared runtime
    factory instead of reusing the parent provider), `agent.max_iterations`,
    `agent.timeout_seconds` (wall clock).

    Progress is reported through `on_event(agent_name, kind, payload)` when a
    callback is supplied, so the TUI can show live sub-agent activity; token
    usage of the sub-loop is recorded on `base_ctx.session_state`.
    """

    async def run_sub(agent, prompt: str, meta: dict[str, Any] | None = None) -> str:
        """Run one sub-agent.

        `meta` (optional, caller-owned) is filled with structured outcome data
        so callers can report per-task elapsed/usage/status instead of
        stitching prose together: `status` ("ok"/"timeout"/"refused"/"error"),
        `elapsed`, `usage`, `model`, `tool_calls`, `error`.
        """
        started = time.monotonic()
        outcome = meta if meta is not None else {}
        outcome.setdefault("status", "ok")
        outcome.setdefault("tool_calls", 0)
        outcome["agent"] = agent.name

        def settle(**values: Any) -> None:
            outcome["elapsed"] = time.monotonic() - started
            outcome.update(values)

        if depth >= SPAWN_DEPTH_LIMIT:
            settle(status="refused")
            return (f"[{agent.name}] refused: sub-agents may not spawn further "
                    f"sub-agents (depth limit {SPAWN_DEPTH_LIMIT})")

        def report(kind: str, payload: dict[str, Any] | None = None) -> None:
            if on_event is not None:
                try:
                    on_event(agent.name, kind, payload or {})
                except Exception:
                    pass  # progress reporting must never break the task

        report("start", {"prompt": prompt[:200]})

        # empty-start registry: only whitelisted tools, never task/parallel_task
        sub_registry = ToolRegistry(tools=[], gate=base_registry.gate)
        for tool in base_registry.tools.values():
            if tool.name not in NON_INHERITED_TOOLS and agent.allows(tool.name):
                sub_registry.add_tool(tool)
        sub_ctx = ToolContext(
            cwd=base_ctx.cwd,
            mode=base_ctx.mode,
            output_limit=base_ctx.output_limit,
            network_mode=getattr(base_ctx, "network_mode", "pull_only"),
            session_allowed=set(base_ctx.session_allowed),
            rules=list(getattr(base_ctx, "rules", []) or []),
            # a COPY of the parent state: without it the sub-agent silently
            # lost checkpointing, spill recovery, outside-read approvals and
            # the token budget — /undo could not revert delegated edits
            session_state=dict(base_ctx.session_state),
            persist_allow=base_ctx.persist_allow,
        )
        # singletons stay with the PARENT: a shallow copy shared live objects
        # (background registry, browser/desktop handles) across concurrent
        # sub-agents — one draining the registry starved the other's notices,
        # and two sub-agents contended one browser. Sub-agents launch fresh.
        # The verify-gate carry is parent-scoped too: a sub-agent's gate must
        # not nag it to verify files the MAIN thread left unverified.
        for singleton in ("background_tasks", "browser_page", "desktop_backend",
                          "verify_pending_paths"):
            sub_ctx.session_state.pop(singleton, None)
        # per-agent thinking dial: injected into the SUB state, so the
        # sub-loop syncs it onto the (possibly shared) provider while the
        # parent is only awaiting this report — the parent's own level is
        # restored by its next pre-call sync. "" = keep the parent's level.
        if getattr(agent, "thinking", ""):
            sub_ctx.session_state["thinking_level"] = agent.thinking
        sub_registry.bind(sub_ctx)

        sub_provider, model_note = _provider_for(agent, provider, config)
        owns_provider = sub_provider is not provider
        system = build_system_prompt(base_ctx.cwd)
        system += f"\n\n# Your role as sub-agent '{agent.name}'\n{agent.system_prompt}\n"
        conversation = Conversation(system_prompt=system)
        bt = budget_tokens() if callable(budget_tokens) else budget_tokens
        sub_kernel = AgentKernel(
            provider=sub_provider,
            registry=sub_registry,
            conversation=conversation,
            max_iterations=agent.max_iterations or max_iterations,
            context_max_tokens=context_max_tokens,
            budget_tokens=bt or 0,
        )
        final = {"text": ""}

        async def drive() -> None:
            async for event in sub_kernel.chat(prompt):
                etype = event.type
                if etype == "turn_completed":
                    final["text"] = str(event.data.get("content") or "")
                elif etype == "tool_call_started":
                    outcome["tool_calls"] = int(outcome.get("tool_calls", 0)) + 1
                    report("tool_start", {"name": event.data.get("name", "")})
                elif etype == "tool_completed":
                    report("tool_end", {"name": event.data.get("name", ""),
                                        "is_error": bool(event.data.get("is_error"))})
                elif etype == "assistant_delta":
                    report("delta", {"text": str(event.data.get("text", ""))[:120]})

        timeout = getattr(agent, "timeout_seconds", 0.0) or 0.0
        try:
            if timeout:
                await asyncio.wait_for(drive(), timeout=timeout)
            else:
                await drive()
        except (TimeoutError, asyncio.TimeoutError):
            report("timeout", {})
            settle(status="timeout", error=f"timed out after {timeout:.0f}s")
            return (f"[{agent.name}] timed out after {timeout:.0f}s without a final "
                    f"answer; partial output was discarded")
        except Exception as exc:
            report("error", {"error": f"{type(exc).__name__}: {exc}"})
            settle(status="error", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            _record_usage(base_ctx, agent.name, sub_kernel, outcome)
            if owns_provider:
                # a per-agent provider was built for THIS task: without this
                # every delegated task leaked an HTTP client (host.aclose
                # only closes the host's own provider/fallbacks)
                aclose = getattr(sub_provider, "aclose", None)
                if callable(aclose):
                    with contextlib.suppress(Exception):
                        await aclose()

        settle(status="ok", model=getattr(agent, "model", "") or "")
        report("done", {})
        text = final["text"] or "(sub-agent produced no final answer)"
        return f"{text}{model_note}"

    return run_sub


def _provider_for(agent, parent_provider, cfg) -> tuple[Any, str]:
    """Per-agent model when declared, else the parent provider."""
    model_id = (getattr(agent, "model", "") or "").strip()
    if not model_id or cfg is None:
        return parent_provider, ""
    try:
        from oaset.config import build_provider, resolve_model

        model_cfg = resolve_model(cfg, model_id)
        return build_provider(cfg, model_cfg), f"\n[model: {model_id}]"
    except Exception as exc:  # unknown/unbuildable model: keep working, say so
        return parent_provider, f"\n[model {model_id} unavailable: {type(exc).__name__}; used parent model]"


def _record_usage(base_ctx, agent_name: str, sub_loop, outcome: dict | None = None) -> None:
    """Accumulate sub-agent token usage on the parent session state.

    Also copies the run's usage onto `outcome` so a caller can show per-task
    numbers (parallel fan-out) instead of only session totals.
    """
    try:
        usage = getattr(sub_loop, "last_usage", None) or {}
        if outcome is not None:
            outcome["usage"] = dict(usage)
        if not usage:
            return
        state = getattr(base_ctx, "session_state", None)
        if state is None:
            return
        totals = state.setdefault("subagent_usage", {})
        bucket = totals.setdefault(agent_name, {"prompt_tokens": 0, "completion_tokens": 0,
                                                "total_tokens": 0, "runs": 0})
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            bucket[key] += int(usage.get(key, 0) or 0)
        bucket["runs"] += 1
    except Exception:
        pass


def call_subagent_runner(runner, agent, prompt: str, meta: dict[str, Any] | None = None):
    """Invoke a sub-agent runner, passing `meta` only when it accepts it.

    `session_state["subagent_runner"]` is a public injection point, so a
    pre-existing two-argument runner (or a user-supplied one) must keep working
    while runners that understood the metadata extension receive it.
    """
    if meta is None:
        return runner(agent, prompt)
    try:
        params = inspect.signature(runner).parameters
    except (TypeError, ValueError):  # builtins / C callables: be conservative
        return runner(agent, prompt)
    accepts_third = len(params) >= 3 or any(
        p.kind is inspect.Parameter.VAR_POSITIONAL for p in params.values())
    if accepts_third:
        return runner(agent, prompt, meta)
    return runner(agent, prompt)
