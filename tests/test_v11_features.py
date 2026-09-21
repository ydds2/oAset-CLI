"""v1.0 features: parallel subagents, budget, tool-output sidecar,
cancel propagation, streaming partial-preserve, legacy console fallback."""

from __future__ import annotations

import asyncio
import json

import pytest

from oaset.agent import AgentLoop, Conversation, initial_system_prompt
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.providers.base import ProviderError, StreamDone
from oaset.tools import AutoGate, ToolContext, ToolRegistry
from oaset.tools.parallel_subagent import ParallelTaskTool


def make_kernel(workspace, script, **kw):
    budget = kw.pop("budget_tokens", 0)
    system_prompt, _ = initial_system_prompt(workspace)
    provider = MockProvider(script)
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=2000))
    from oaset.kernel import AgentKernel as K

    kernel = K(provider=provider, registry=registry,
               conversation=Conversation(system_prompt),
               max_iterations=8, budget_tokens=budget, **kw)
    return kernel, provider


# ------------------------------------------------------- parallel subagents


async def test_parallel_task_runs_concurrently(workspace):
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=3000)
    registry.bind(ctx)

    async def runner(agent, prompt):
        return f"report for {prompt}"

    ctx.session_state["subagent_runner"] = runner
    tool = ParallelTaskTool()
    result = await tool.run({
        "tasks": [
            {"description": "research A", "prompt": "do A"},
            {"description": "research B", "prompt": "do B"},
        ]}, ctx)
    assert not result.is_error
    assert "do A" in result.output and "do B" in result.output


async def test_parallel_task_via_registry(workspace):
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=3000)
    registry.bind(ctx)

    async def runner(agent, prompt):
        return f"report: {prompt}"

    ctx.session_state["subagent_runner"] = runner
    result = await registry.dispatch("parallel_task", json.dumps({
        "tasks": [{"prompt": "task one"}, {"prompt": "task two"}]}))
    assert not result.is_error
    assert "task one" in result.output and "task two" in result.output


async def test_parallel_task_rejects_more_than_four(workspace):
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=200)
    registry.bind(ctx)

    async def runner(agent, prompt):
        return "r"

    ctx.session_state["subagent_runner"] = runner
    tool = ParallelTaskTool()
    result = await tool.run({"tasks": [{"prompt": f"t{i}"} for i in range(5)]}, ctx)
    assert result.is_error and "At most 4" in result.output


# ------------------------------------------------------------------- budget


async def test_budget_exhausted_blocks_run(workspace):
    provider = MockProvider([MockTurn(content_chunks=["ok"],
                                      usage={"total_tokens": 500})])
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=200))
    from oaset.agent.loop import AgentLoop

    loop = AgentLoop(provider, registry, Conversation(system_prompt="s"),
                     budget_tokens=300)
    events: list[dict] = []
    await loop.run("turn one", events.append)
    assert any(e["type"] == "budget_exhausted" for e in events)

    events2: list[dict] = []
    await loop.run("turn two", events2.append)
    blocked = [e for e in events2 if e["type"] == "budget_exhausted"]
    assert blocked and blocked[0].get("blocked") is True
    assert len(events2) == 1  # nothing else ran


# -------------------------------------------------- tool output sidecar


async def test_tool_output_sidecar_written(isolated_home, workspace):
    big = "x" * 5000
    (workspace / "big.txt").write_text(big, encoding="utf-8")
    store_mod = __import__("oaset.session.store", fromlist=["SessionStore"])
    store = store_mod.SessionStore()
    session = store.new_session(workspace, model="m")
    out_dir = session.file.parent / "tool-outputs"

    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("read_file", {"path": "big.txt"})]),
        MockTurn(content_chunks=["done"]),
    ])
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=8000))
    registry.ctx.session_state["tool_output_dir"] = str(out_dir)
    loop = AgentLoop(provider, registry, Conversation(system_prompt="s"), max_iterations=5)
    events: list[dict] = []
    await loop.run("read big", events.append)

    sidecars = list(out_dir.glob("*.txt"))
    assert sidecars, "full tool output should be written to sidecar"
    side_text = sidecars[0].read_text(encoding="utf-8")
    assert big in side_text  # full (untruncated) content preserved
    tool_msg = [m for m in loop.conversation.messages if m.role == "tool"][0]
    assert "[完整输出已保存" in tool_msg.content


# ------------------------------------------------- cancel propagation


async def test_cancel_closes_provider_connection(workspace):
    closed = {"flag": False}

    class ClosingProvider:
        model_id = "mock/closing"

        async def stream(self, messages, tools):
            try:
                for i in range(60):
                    if await asyncio.sleep(0.05):
                        pass
                    from oaset.providers.base import ContentDelta

                    yield ContentDelta(text=f"c{i} ")
            finally:
                closed["flag"] = True

    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=200))
    loop = AgentLoop(ClosingProvider(), registry, Conversation(system_prompt="s"))
    task = asyncio.create_task(loop.run("hi", lambda e: None))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)
    assert closed["flag"] is True  # 中断传播：provider 连接被关闭


# --------------------------------------- streaming partial-preserve


class DyingProvider:
    """First attempt: yield a chunk then die mid-stream (retryable).
    Second attempt: recover and finish."""

    model_id = "mock/dying"

    def __init__(self) -> None:
        self.attempts = 0
        self.requests: list[list] = []

    async def stream(self, messages, tools):
        self.attempts += 1
        self.requests.append(list(messages))
        if self.attempts == 1:
            from oaset.providers.base import ContentDelta

            yield ContentDelta(text="partial keep ")
            raise ProviderError("network", "connection reset", retryable=True)
        from oaset.providers.base import ContentDelta

        yield ContentDelta(text="recovered full")
        yield StreamDone(finish_reason="stop", usage=None)


async def test_streaming_breakpoint_preserves_partial(workspace):
    from oaset.agent.loop import AgentLoop

    provider = DyingProvider()
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=300))
    import oaset.agent.loop as loop_mod

    saved = loop_mod.RETRY_BACKOFF[:]
    loop_mod.RETRY_BACKOFF[:] = [0]  # no sleep in test
    try:
        loop = AgentLoop(provider, registry, Conversation(system_prompt="s"))
        events: list[dict] = []
        await loop.run("go", events.append)
        kinds = [e["type"] for e in events]
        assert "content_delta" in kinds
        partial_text = "".join(
            e.get("text", "") for e in events if e["type"] == "content_delta")
        assert "partial keep " in partial_text
        assert "recovered full" in partial_text  # 保留的内容 + 重试续写
        assert events[-1]["content"].endswith("recovered full")
        assert provider.attempts == 2
    finally:
        loop_mod.RETRY_BACKOFF[:] = saved


# -------------------------------------------- legacy console fallback


def test_legacy_console_class_and_hint():

    from oaset.tui.widgets.status_bar import clip, fmt_k

    assert clip("hello", 3) == "hel"
    assert clip("中文中文", 5) == "中文"  # 5 cells -> 2 CJK chars  # CJK double-width aware
    assert fmt_k(262144) == "262.1k"
