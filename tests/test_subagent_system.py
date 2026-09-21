"""Sub-agent system: richer definitions, per-agent model, progress, accounting.

Covers the gaps that existed before: no per-agent model, no recursion cap for
parallel fan-out, silent fallback for unknown agent names, no token
accounting, no live progress, no create/delete/reload surface.
"""

from __future__ import annotations

import asyncio

import pytest

from oaset.agent.subagent_runner import make_subagent_runner
from oaset.agents import (
    BUILTIN_GENERAL,
    SPAWN_DEPTH_LIMIT,
    create_agent,
    delete_agent,
    find_agent,
    load_agents,
)
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.tools import AutoGate, ToolContext, ToolRegistry


def write_agents(workspace, isolated_home) -> None:
    project = workspace / ".oaset" / "agents"
    project.mkdir(parents=True, exist_ok=True)
    (project / "scout.md").write_text(
        "name: Scout\n"
        "description: Explores the repo\n"
        "tools: read_file, list_dir\n"
        "model: mock/mock-echo\n"
        "max_iterations: 3\n"
        "timeout_seconds: 5\n"
        "\n"
        "You explore and summarize.\n",
        encoding="utf-8",
    )
    user = isolated_home / "agents"
    user.mkdir(parents=True, exist_ok=True)
    (user / "scout.md").write_text(
        "name: Scout\ndescription: global scout\n\nGlobal prompt.\n", encoding="utf-8")
    (user / "reviewer.md").write_text(
        "name: Reviewer\ndescription: reviews code\n\nReview carefully.\n", encoding="utf-8")


# ------------------------------------------------------------- definitions


def test_project_agent_overrides_user_agent(workspace, isolated_home):
    write_agents(workspace, isolated_home)
    agents = {a.name.lower(): a for a in load_agents(workspace)}
    assert agents["scout"].scope == "project"       # project wins over global
    assert agents["scout"].description == "Explores the repo"
    assert agents["reviewer"].scope == "user"        # untouched global entry


def test_agent_header_parses_model_limits_and_tools(workspace, isolated_home):
    write_agents(workspace, isolated_home)
    scout = find_agent(workspace, "scout")
    assert scout is not None
    assert scout.model == "mock/mock-echo"
    assert scout.max_iterations == 3
    assert scout.timeout_seconds == 5
    assert scout.tools == ["read_file", "list_dir"]
    assert not scout.allows("run_shell")


def test_agent_header_description_not_lost_when_body_empty(workspace, isolated_home):
    path = workspace / ".oaset" / "agents"
    path.mkdir(parents=True, exist_ok=True)
    (path / "brief.md").write_text("name: Brief\ndescription: header only\n", encoding="utf-8")
    agent = find_agent(workspace, "brief")
    assert agent is not None and agent.description == "header only"


def test_create_and_delete_agent(workspace, isolated_home):
    path = create_agent(workspace, "tester", "runs the tests", scope="project",
                        model="mock/mock-echo")
    assert path.is_file() and path.name == "tester.md"
    agent = find_agent(workspace, "tester")
    assert agent is not None and agent.model == "mock/mock-echo"

    with pytest.raises(ValueError):
        create_agent(workspace, "tester", scope="project")  # no overwrite
    create_agent(workspace, "tester", scope="project", overwrite=True)

    removed = delete_agent(workspace, "tester")
    assert removed is not None and not removed.exists()
    assert find_agent(workspace, "tester") is None


def test_create_agent_validates_name_and_scope(workspace, isolated_home):
    with pytest.raises(ValueError):
        create_agent(workspace, "", scope="project")
    with pytest.raises(ValueError):
        create_agent(workspace, "bad/name", scope="project")
    with pytest.raises(ValueError):
        create_agent(workspace, "ok", scope="somewhere")


def test_unknown_agent_name_is_an_error_not_a_silent_fallback(workspace, isolated_home):
    from oaset.tools.subagent import TaskTool

    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=500)
    registry.bind(ctx)
    ctx.session_state["agents"] = {}                    # nothing defined
    ctx.session_state["subagent_runner"] = _noop_runner
    registry.add_tool(TaskTool())

    result = asyncio.run(registry.dispatch("task", '{"agent": "ghost", "prompt": "x"}'))
    assert result.is_error and "unknown sub-agent" in result.output
    # the built-in general still works
    ok = asyncio.run(registry.dispatch("task", '{"agent": "general", "prompt": "x"}'))
    assert not ok.is_error


async def _noop_runner(agent, prompt, meta=None):
    if meta is not None:
        meta.update(status="ok", elapsed=0.01, tool_calls=0)
    return f"[{agent.name}] {prompt}"


# --------------------------------------------------- runner behavior


def _ctx(workspace, registry):
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=1000,
                      network_mode="pull_only")
    registry.bind(ctx)
    return ctx


def test_runner_strips_fanout_tools_from_subagents(workspace, isolated_home):
    """task AND parallel_task must not be inherited (depth cap)."""
    from oaset.tools.parallel_subagent import ParallelTaskTool
    from oaset.tools.subagent import TaskTool

    registry = ToolRegistry(gate=AutoGate())
    for tool in (TaskTool(), ParallelTaskTool()):
        registry.add_tool(tool)
    ctx = _ctx(workspace, registry)
    seen: dict = {}

    captured = {}

    def fake_run(prompt, emit):
        captured["tools"] = set()  # placeholder; asserted via registry below

    runner = make_subagent_runner(MockProvider([]), registry, ctx, depth=0)
    seen["runner"] = runner

    # assert through the registry the runner builds by intercepting AgentKernel
    # (the runner migrated from AgentLoop to the kernel; same interception)
    import oaset.agent.subagent_runner as runner_mod
    real_loop = runner_mod.AgentKernel

    class SpyLoop(real_loop):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            captured["tools"] = set(kw["registry"].tools)
            super().__init__(*a, **kw)

    runner_mod.AgentKernel = SpyLoop
    try:
        asyncio.run(runner(BUILTIN_GENERAL, "hi"))
    finally:
        runner_mod.AgentKernel = real_loop
    assert "task" not in captured["tools"]
    assert "parallel_task" not in captured["tools"]


def test_runner_refuses_when_depth_limit_reached(workspace, isolated_home):
    registry = ToolRegistry(gate=AutoGate())
    ctx = _ctx(workspace, registry)
    runner = make_subagent_runner(MockProvider([]), registry, ctx, depth=SPAWN_DEPTH_LIMIT)
    report = asyncio.run(runner(BUILTIN_GENERAL, "hi"))
    assert "refused" in report and "depth" in report


def test_runner_reports_progress_events(workspace, isolated_home):
    (workspace / "note.txt").write_text("payload", encoding="utf-8")
    registry = ToolRegistry(gate=AutoGate())
    ctx = _ctx(workspace, registry)
    seen: list[tuple[str, str]] = []

    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("read_file", {"path": "note.txt"})]),
        MockTurn(content_chunks=["done"]),
    ])
    runner = make_subagent_runner(provider, registry, ctx,
                                  on_event=lambda name, kind, payload: seen.append((name, kind)))
    report = asyncio.run(runner(BUILTIN_GENERAL, "read it"))
    kinds = [kind for _, kind in seen]
    assert "start" in kinds and "tool_start" in kinds and "done" in kinds
    assert "done" in report


def test_runner_records_token_usage(workspace, isolated_home):
    registry = ToolRegistry(gate=AutoGate())
    ctx = _ctx(workspace, registry)
    provider = MockProvider([
        MockTurn(content_chunks=["answer"],
                 usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}),
    ])
    runner = make_subagent_runner(provider, registry, ctx)
    asyncio.run(runner(BUILTIN_GENERAL, "hi"))
    usage = ctx.session_state.get("subagent_usage", {})
    assert usage.get("general", {}).get("total_tokens") == 10
    assert usage["general"]["runs"] == 1


def test_runner_applies_wall_clock_timeout(workspace, isolated_home):
    registry = ToolRegistry(gate=AutoGate())
    ctx = _ctx(workspace, registry)

    class SlowProvider(MockProvider):
        async def stream(self, messages, tools=None, **kw):
            await asyncio.sleep(5)
            async for item in super().stream(messages, tools, **kw):  # pragma: no cover
                yield item

    agent = BUILTIN_GENERAL
    object.__setattr__ if False else None
    from oaset.agents import AgentDef

    slow_agent = AgentDef(name="slow", description="", system_prompt="", timeout_seconds=0.05)
    runner = make_subagent_runner(SlowProvider([]), registry, ctx)
    report = asyncio.run(runner(slow_agent, "hi"))
    assert "timed out" in report
    _ = agent


# ------------------------------------------- progress vocabulary contract

def test_subagent_event_kinds_match_what_the_runner_emits():
    """SUBAGENT_EVENT_KINDS is the declared contract for on_event
    consumers; every literal the runner emits must be in it, and (the
    reverse drift) nothing declared may be unrendered by the TUI."""
    import inspect
    import re

    from oaset.agent.subagent_runner import SUBAGENT_EVENT_KINDS

    source = inspect.getsource(
        __import__("oaset.agent.subagent_runner", fromlist=["x"]))
    emitted = set(re.findall(r'report\("([a-z_]+)"', source))
    assert emitted and emitted <= set(SUBAGENT_EVENT_KINDS), (
        f"runner emits kinds outside the declared vocabulary: "
        f"{sorted(emitted - set(SUBAGENT_EVENT_KINDS))}")


def test_tui_renders_every_kind_except_delta(workspace, isolated_home):
    """The live card must have a branch for each declared kind except
    delta (deliberately too chatty); 'error' used to fall through
    silently — a dying sub-agent left no live trace."""
    import inspect

    from oaset.agent.subagent_runner import SUBAGENT_EVENT_KINDS
    from oaset.tui import app as app_mod

    source = inspect.getsource(app_mod.OasetApp._on_subagent_event)
    for kind in SUBAGENT_EVENT_KINDS:
        if kind == "delta":
            continue
        assert f'"{kind}"' in source, f"no live branch renders kind {kind}"
