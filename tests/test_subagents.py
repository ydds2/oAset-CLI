"""Sub-agents: markdown loader, task tool, runner factory, prompt section."""

from __future__ import annotations

from oaset.agent import AgentLoop, Conversation, initial_system_prompt
from oaset.agent.subagent_runner import make_subagent_runner
from oaset.agents import BUILTIN_GENERAL, find_agent, load_agents
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.tools import AutoGate, ToolContext, ToolRegistry


def make_agents(workspace, isolated_home):
    home_agents = isolated_home / "agents"
    home_agents.mkdir(parents=True)
    (home_agents / "reviewer.md").write_text(
        "name: Reviewer\n"
        "description: Reviews code changes for defects\n"
        "tools: read_file, glob, grep\n"
        "\n"
        "You are a meticulous code reviewer. Report findings only.\n",
        encoding="utf-8",
    )
    project = workspace / ".oaset" / "agents"
    project.mkdir(parents=True)
    (project / "scout.md").write_text(
        "Scout explores the repo and summarizes its structure.\n",
        encoding="utf-8",
    )


def test_load_and_find_agents(workspace, isolated_home):
    make_agents(workspace, isolated_home)
    agents = load_agents(workspace)
    names = {a.name for a in agents}
    assert names == {"Reviewer", "scout"}
    reviewer = find_agent(workspace, "reviewer")
    assert reviewer is not None
    assert reviewer.allows("read_file") and not reviewer.allows("run_shell")
    scout = find_agent(workspace, "SCOUT")
    assert scout is not None and scout.tools == []  # no header → all tools
    assert "summarizes" in scout.system_prompt
    assert find_agent(workspace, "missing") is None


def test_builtin_general():
    assert BUILTIN_GENERAL.allows("run_shell")
    assert "sub-agent" in BUILTIN_GENERAL.system_prompt


def test_system_prompt_lists_agents(workspace, isolated_home):
    make_agents(workspace, isolated_home)
    prompt, _ = initial_system_prompt(workspace)
    assert "Sub-agents" in prompt
    assert "Reviewer" in prompt and "task tool" in prompt


async def test_task_tool_via_runner(workspace, isolated_home):
    make_agents(workspace, isolated_home)
    (workspace / "data.txt").write_text("the-data", encoding="utf-8")
    sub_script = [
        MockTurn(content_chunks=["checking"], tool_calls=[MockToolCall("read_file", {"path": "data.txt"})]),
        MockTurn(content_chunks=["SUB-REPORT: found the-data"]),
    ]
    _provider = MockProvider([])  # main provider unused here
    sub_provider = MockProvider(sub_script)
    _conversation = Conversation(system_prompt="sys")
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=500)
    registry.bind(ctx)
    ctx.session_state["agents"] = {a.name.lower(): a for a in load_agents(workspace)}
    runner = make_subagent_runner(
        sub_provider, registry, ctx, max_iterations=5, context_max_tokens=32000
    )
    ctx.session_state["subagent_runner"] = runner

    result = await registry.dispatch(
        "task",
        '{"agent": "scout", "description": "explore", "prompt": "read data.txt"}',
    )
    assert not result.is_error
    assert "[scout]" in result.output and "SUB-REPORT" in result.output
    # the sub loop made two provider calls (tool turn + final turn)
    assert len(sub_provider.requests) == 2


async def test_task_recursion_guard(workspace, isolated_home):
    _provider = MockProvider([])
    conversation = Conversation(system_prompt="sys")
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=500)
    registry.bind(ctx)
    ctx.session_state["agents"] = {}
    # the sub-loop must not contain the task tool in its schema payload
    captured = {}

    class SpyProvider(MockProvider):
        async def stream(self, messages, tools):
            captured["tools"] = [t["function"]["name"] for t in (tools or [])]
            async for event in super().stream(messages, tools):
                yield event

    spy = SpyProvider([MockTurn(content_chunks=["done"])])
    ctx2 = ToolContext(cwd=workspace, mode="auto", output_limit=500)
    runner2 = make_subagent_runner(spy, registry, ctx2, max_iterations=5)
    report = await runner2(BUILTIN_GENERAL, "hello")
    assert report  # produced an answer
    assert "task" not in captured["tools"]
    assert conversation.messages == []  # main conversation untouched by sub run


async def test_task_without_runner_errors(workspace):
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=500)
    registry.bind(ctx)
    result = await registry.dispatch("task", '{"prompt": "x"}')
    assert result.is_error and "not available" in result.output


async def test_loop_end_to_end_with_task_tool(workspace, isolated_home):
    """Main loop: model calls task → sub-agent runs → report flows back as tool result."""
    make_agents(workspace, isolated_home)  # the named agent must really exist
    (workspace / "note.txt").write_text("hello from note", encoding="utf-8")
    main_provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("task", {
            "agent": "scout",
            "description": "read note",
            "prompt": "Read note.txt and report its content.",
        })]),
        MockTurn(content_chunks=["The sub-agent reported: hello from note"]),
    ])
    sub_provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("read_file", {"path": "note.txt"})]),
        MockTurn(content_chunks=["hello from note"]),
    ])
    system_prompt, _ = initial_system_prompt(workspace)
    conversation = Conversation(system_prompt)
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=1000)
    registry.bind(ctx)
    ctx.session_state["agents"] = {a.name.lower(): a for a in load_agents(workspace)}
    ctx.session_state["subagent_runner"] = make_subagent_runner(
        sub_provider, registry, ctx, max_iterations=5
    )
    loop = AgentLoop(main_provider, registry, conversation, max_iterations=5)
    events: list[dict] = []
    await loop.run("delegate reading the note", events.append)
    tool_end = [e for e in events if e["type"] == "tool_end"]
    assert tool_end and "hello from note" in tool_end[-1]["result"]
    assert "The sub-agent reported" in events[-1]["content"]
