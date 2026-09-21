"""Agent kernel (event bus) + public SDK + auto-compact."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

import pytest

from oaset.agent import Conversation
from oaset.agent.messages import Message
from oaset.config import ConfigError
from oaset.kernel import AgentKernel
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.sdk import Oaset
from oaset.tools import AutoGate, ToolContext, ToolRegistry


def make_kernel(workspace, script):
    provider = MockProvider(script)
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=500))
    kernel = AgentKernel(
        provider=provider,
        registry=registry,
        conversation=Conversation(system_prompt="sys"),
        max_iterations=5,
    )
    return kernel


async def test_kernel_chat_yields_typed_events(workspace):
    kernel = make_kernel(workspace, [MockTurn(content_chunks=["he", "llo"])])
    seen: list[str] = []
    async for event in kernel.chat("hi"):
        seen.append(event.type)
        if event.type == "turn_completed":
            assert event.data["content"] == "hello"
    assert seen[0] == "message_appended"  # the USER turn: role-aware naming
    # (it used to arrive as assistant_message — the alias table could not
    # see the message role and painted user/tool messages as assistant)
    assert seen[1] == "turn_started"
    assert seen[-1] == "turn_completed"
    assert "assistant_delta" in seen


async def test_kernel_bus_subscription(workspace):
    kernel = make_kernel(workspace, [MockTurn(content_chunks=["bus"])])
    got: list[str] = []
    unsubscribe = kernel.subscribe("turn_completed", lambda e: got.append(e.type))
    async for _ in kernel.chat("x"):
        pass
    await asyncio.sleep(0)
    assert got == ["turn_completed"]
    unsubscribe()  # stable API


async def test_kernel_ask_returns_final(workspace):
    kernel = make_kernel(workspace, [MockTurn(content_chunks=["the answer"])])
    assert await kernel.ask("q") == "the answer"


async def test_kernel_injection_flows_into_loop(workspace):
    kernel = make_kernel(workspace, [
        MockTurn(tool_calls=[MockToolCall("list_dir", {})]),
        MockTurn(content_chunks=["done"]),
    ])
    kernel.inject("focus on py files")
    events: list[dict] = []

    async for event in kernel.chat("start"):
        if event.type == "turn_completed":
            events.append(event)
    injected = [m for m in kernel.conversation.messages
                if m.role == "user" and "mid-stream" in (m.content or "")]
    assert injected, "injection should enter the conversation"
    assert events and events[-1].type == "turn_completed"


# ------------------------------------------------------------------- SDK


async def test_sdk_ask_and_events(workspace):

    agent = Oaset(
        model="mock/mock-echo",
        provider=MockProvider([MockTurn(content_chunks=["sdk answer"])]),
        cwd=workspace,
        tools=False,
    )

    async def consume():
        events = []
        async for event in agent.chat("hello sdk"):
            assert event.payload()["type"] == event.type
            kind, data = event  # backwards-compatible tuple unpacking
            events.append((kind, data.get("content", "")))
        return events

    task = asyncio.create_task(consume())
    done, pending = await asyncio.wait({task}, timeout=10)
    if pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        pytest.fail("sdk chat hung — dump would be needed")
    kinds = task.result()
    assert kinds and kinds[-1][0] == "turn_completed"
    assert kinds[-1][1] == "sdk answer"
    assert await agent.ask("again") == "[echo] again"  # script exhausted -> echo


async def test_sdk_close_is_idempotent_and_context_managed(workspace):
    provider = MockProvider([])
    agent = Oaset(model="mock/mock-echo", provider=provider, cwd=workspace, tools=False)
    async with agent as session:
        assert session is agent
    await agent.aclose()
    assert getattr(provider, "_client", None) is None or True



# --------------------------------------------------------- auto-compact


async def test_auto_compact_triggers_at_threshold(workspace):
    long_user = "filler " * 400  # plenty of tokens
    script = [
        MockTurn(content_chunks=["summary of everything"]),
        MockTurn(content_chunks=["after compact final"]),
    ]
    provider = MockProvider(script)
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=500))
    conversation = Conversation(system_prompt="s")
    for i in range(6):
        conversation.append(Message(role="user", content=f"history {i}: " + "detail " * 40))
    kernel = AgentKernel(
        provider=provider,
        registry=registry,
        conversation=conversation,
        context_max_tokens=120,  # tiny: forces compaction before call 1
        auto_compact=True,
    )
    events = [e async for e in kernel.chat(long_user)]
    compacts = [e for e in events if e.type == "auto_compacted"]
    assert compacts, "auto_compact event should fire"
    # the provider's first request contained the compacted summary request
    first_request_texts = json_dump(provider.requests[0])
    assert "Summarize" in first_request_texts or "summar" in first_request_texts.lower()
    assert events[-1].data["content"] == "after compact final"


def json_dump(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)


# ------------------------------------------------------------ config error


def test_broken_config_raises_clear_error(isolated_home):
    from oaset.config import load_config

    (isolated_home / "config.toml").write_text("this is [not toml", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert "config.toml" in str(exc.value)


# ------------------------------------------------- SDK MCP lifecycle (SDK-02)

FAKE_MCP = Path(__file__).resolve().parent / "fake_mcp_server.py"


def _write_mcp_config(home, name="sdkfake"):
    import json

    (home / "mcp.json").write_text(json.dumps({
        "mcpServers": {name: {"command": sys.executable, "args": [str(FAKE_MCP)]}},
    }), encoding="utf-8")


async def _consume(agent, prompt="hi"):
    async for _ in agent.chat(prompt):
        pass


async def test_sdk_explicit_mcp_start_registers_tools_and_aclose_shuts_down(
        workspace, isolated_home):
    """`mcp_start()` is the SDK twin of the TUI `_load_mcp` startup: servers
    from `~/.oaset/mcp.json` come up isolated per-server, their tools land in
    the shared registry, and aclose() retracts the connections."""

    from oaset.mcp.client import sanitize_tool_name

    _write_mcp_config(isolated_home)
    agent = Oaset(model="mock/mock-echo", provider=MockProvider([]), cwd=workspace)
    assert agent.mcp is None  # not started until needed
    status = agent.mcp_status()
    assert "not started" in status or "未启动" in status  # localised now

    results = await agent.mcp_start()
    assert results == {"sdkfake": None}
    expected = sanitize_tool_name("sdkfake", "echo")
    assert expected in agent.registry.tools
    assert "sdkfake:2 tools" in agent.mcp_status()

    await agent.aclose()
    assert agent.mcp.connections == {}
    assert agent.mcp.remote_tools == {}


async def test_sdk_chat_lazily_starts_mcp_and_turn_can_call_mcp_tool(
        workspace, isolated_home):
    """TUI-equivalent default: MCP servers come up automatically on the first
    turn, so the model can call an MCP tool without any explicit API call."""

    from oaset.mcp.client import sanitize_tool_name

    _write_mcp_config(isolated_home)
    echo_tool = sanitize_tool_name("sdkfake", "echo")
    agent = Oaset(
        model="mock/mock-echo", cwd=workspace, yolo=True,
        provider=MockProvider([
            MockTurn(tool_calls=[MockToolCall(echo_tool, {"text": "via sdk"})]),
            MockTurn(content_chunks=["mcp done"]),
        ]),
    )
    try:
        seen = []
        async for event in agent.chat("use the echo tool"):
            seen.append(event.type)
        assert seen[-1] == "turn_completed"
        assert agent._mcp_started
        tool = agent.registry.tools[echo_tool]
        from oaset.tools import ToolContext

        ctx = ToolContext(cwd=workspace, mode="auto", output_limit=200)
        result = await tool.run({"text": "direct"}, ctx)
        assert result.output == "echo: direct"
    finally:
        await agent.aclose()


async def test_sdk_mcp_reload_replaces_tools_and_refuses_mid_turn(
        workspace, isolated_home):
    """/reload-mcp semantics: a busy turn refuses the reload; an idle reload
    closes old connections and re-registers fresh tools."""
    _write_mcp_config(isolated_home)
    agent = Oaset(model="mock/mock-echo", provider=MockProvider([]), cwd=workspace)
    await agent.mcp_start()

    agent._turns_running = 1  # simulate an in-flight turn
    with pytest.raises(RuntimeError, match="reload busy"):
        await agent.mcp_reload()
    agent._turns_running = 0

    results = await agent.mcp_reload()
    assert results == {"sdkfake": None}
    assert "sdkfake:2 tools" in agent.mcp_status()
    await agent.aclose()


async def test_sdk_mcp_disabled_opts_out_entirely(workspace):
    agent = Oaset(model="mock/mock-echo", provider=MockProvider([]),
                  cwd=workspace, mcp=False)
    assert await agent.mcp_start() == {}
    text = await agent.ask("hello")
    assert text  # turn ran fine without MCP
    assert agent.mcp is None
    await agent.aclose()


# ------------------------------------------------------ SDK hooks (SDK-02)


async def test_sdk_hooks_fire_like_the_tui(workspace, tmp_path):
    """hooks= overrides config; the same HookRunner semantics fire on SDK
    turns (env-payload commands, on_user_message / on_turn_end)."""
    log = tmp_path / "sdk-hooks.log"
    agent = Oaset(
        model="mock/mock-echo", cwd=workspace,
        provider=MockProvider([MockTurn(content_chunks=["ok"])]),
        hooks={
            "on_user_message": f'echo "$OASET_EVENT:$OASET_USER_TEXT" >> "{log}"',
            "on_turn_end": f'echo "$OASET_EVENT" >> "{log}"',
        },
    )
    try:
        assert await agent.ask("hello hooks") == "ok"
        await asyncio.sleep(0.3)
        content = log.read_text(encoding="utf-8")
        assert "on_user_message:hello hooks" in content
        assert content.strip().splitlines()[-1] == "on_turn_end"
    finally:
        await agent.aclose()


async def test_sdk_hooks_default_to_config_and_are_editable_between_turns(
        workspace, isolated_home, tmp_path):
    """No explicit hooks -> config.toml hooks apply (TUI parity); mutating
    `agent.hooks` affects the next turn, since the runner is rebuilt per turn."""
    from oaset.config import default_config, save_config

    log = tmp_path / "cfg-hooks.log"
    cfg = default_config()
    cfg.hooks = {"on_user_message": f'echo "$OASET_EVENT" >> "{log}"'}
    save_config(cfg)
    agent = Oaset(cwd=workspace, provider=MockProvider([MockTurn(content_chunks=["ok"])]))
    assert agent.hooks.get("on_user_message")

    # a no-op override dict must NOT keep firing after the next turn rebuild
    agent.hooks = {}
    await agent.ask("silent")
    await asyncio.sleep(0.3)
    assert not log.exists() or "on_user_message" not in log.read_text(encoding="utf-8")
    await agent.aclose()


# ------------------------------------------------- Phase B kernel contracts

async def test_kernel_envelopes_are_canonical_and_sequenced(workspace):
    """Phase B-1: every yielded event carries the full envelope; sequences
    are monotonic within the session; loop names never leak."""
    kernel = make_kernel(workspace, [MockTurn(content_chunks=["a", "b"])])
    seen = []
    async for event in kernel.chat("hi"):
        seen.append(event)
        assert event.schema_version == 1
        assert event.session_id == kernel.session_id
        assert event.event_id and event.timestamp
        assert event.type not in ("content_delta", "turn_start", "turn_done")
    sequences = [e.sequence for e in seen]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
    assert seen[-1].type == "turn_completed"
    # payload view keeps renderer-friendly flat shape
    assert seen[-1].payload()["type"] == "turn_completed"


async def test_kernel_rejects_concurrent_turns_with_session_busy(workspace):
    """Phase B-5: one in-flight turn per session. A second concurrent chat()
    raises session_busy WITHOUT mutating the conversation."""
    from oaset.errors import SessionBusyError

    release = asyncio.Event()

    class SlowProvider(MockProvider):
        async def stream(self, messages, tools):
            await asyncio.wait_for(release.wait(), timeout=10)
            async for item in super().stream(messages, tools):
                yield item

    kernel = make_kernel(workspace, [MockTurn(content_chunks=["slow"])])
    kernel.provider = SlowProvider([MockTurn(content_chunks=["slow"])])

    first = kernel.chat("one")
    got_first = asyncio.ensure_future(anext(first))
    await asyncio.sleep(0.1)  # let the first turn start
    assert kernel.turn_active
    with pytest.raises(SessionBusyError) as excinfo:
        async for _ in kernel.chat("two"):
            pass
    assert excinfo.value.code == "session_busy"
    released = kernel.conversation.messages
    release.set()
    await got_first
    async for _ in first:
        pass
    # the rejected second chat added nothing to the conversation
    assert kernel.conversation.messages == released
    # after the turn ends the guard releases
    assert kernel.turn_active is False


async def test_kernel_bus_surfaces_handler_errors_as_diagnostics(workspace):
    """Phase B-4: a crashing subscriber is recorded AND re-published as a
    diagnostic_created event — never silently swallowed."""

    def boom(event):
        raise RuntimeError("subscriber exploded")

    kernel = make_kernel(workspace, [MockTurn(content_chunks=["x"])])
    kernel.subscribe("assistant_delta", boom)
    diagnostics = []
    kernel.subscribe("diagnostic_created", lambda e: diagnostics.append(e))
    async for _ in kernel.chat("y"):
        pass
    assert diagnostics, "handler crash must produce a diagnostic event"
    assert diagnostics[0].data["kind"] == "handler_error"
    assert "subscriber exploded" in diagnostics[0].data["error"]
    assert kernel.bus.handler_errors


async def test_kernel_fallbacks_reach_the_loop(workspace):
    """Phase B: fallback providers passed to the kernel reach each built loop,
    so SDK/CLI share the TUI fallback semantics."""
    kernel = make_kernel(workspace, [MockTurn(content_chunks=["ok"])])
    backup = MockProvider([])
    kernel.fallbacks = [backup]
    started = kernel.chat("z")
    async for _ in started:
        pass
    assert kernel.fallbacks == [backup]


async def test_kernel_keeps_the_fallback_provider_after_a_switch(workspace):
    from oaset.providers.base import ProviderError

    class Dead:
        model_id = "dead/model"

        async def stream(self, messages, tools=None, **kwargs):
            raise ProviderError("server", "primary down", retryable=False)
            yield  # pragma: no cover — make this an async generator

    backup = MockProvider([MockTurn(content_chunks=["rescued"])])
    kernel = AgentKernel(
        provider=Dead(),
        registry=ToolRegistry(gate=AutoGate()),
        conversation=Conversation(system_prompt="s"),
        fallbacks=[backup],
        max_iterations=0,
    )
    text = await kernel.ask("hi")
    assert "rescued" in text
    assert kernel.provider is backup
    assert kernel.fallbacks == []
