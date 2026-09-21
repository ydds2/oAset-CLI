"""AgentLoop integration: scripted turns, budget, injection, clean interruption."""

from __future__ import annotations

import asyncio

from oaset.agent import AgentLoop, Conversation, initial_system_prompt
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.providers.base import ProviderError
from oaset.tools import AutoGate, ToolContext, ToolRegistry


def build(workspace, provider, max_iterations=25):
    system_prompt, _ = initial_system_prompt(workspace)
    conversation = Conversation(system_prompt)
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=1000))
    loop = AgentLoop(provider, registry, conversation, max_iterations=max_iterations)
    return loop, conversation


async def test_full_tool_cycle(workspace):
    (workspace / "note.txt").write_text("hello from note", encoding="utf-8")
    provider = MockProvider(
        [
            MockTurn(
                content_chunks=["Let me look."],
                tool_calls=[MockToolCall("read_file", {"path": "note.txt"})],
            ),
            MockTurn(content_chunks=["The note says: hello from note"]),
        ]
    )
    loop, conversation = build(workspace, provider)
    events: list[dict] = []
    await loop.run("what does the note say?", events.append)

    kinds = [e["type"] for e in events]
    assert kinds[0] == "message_done"  # persisted user message
    assert "turn_start" in kinds
    assert kinds.count("content_delta") >= 1
    assert "tool_start" in kinds and "tool_end" in kinds
    assert kinds[-1] == "turn_done"
    assert "hello from note" in events[-1]["content"]

    roles = [m.role for m in conversation.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    wire = conversation.wire_messages()
    assert wire[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert wire[3]["role"] == "tool" and "hello from note" in wire[3]["content"]


async def test_iteration_budget(workspace):
    provider = MockProvider(
        [
            MockTurn(tool_calls=[MockToolCall("list_dir", {})]),
            MockTurn(tool_calls=[MockToolCall("list_dir", {})]),
        ]
    )
    loop, conversation = build(workspace, provider, max_iterations=2)
    events: list[dict] = []
    await loop.run("go", events.append)
    kinds = [e["type"] for e in events]
    assert "iterations_exhausted" in kinds
    assert kinds[-1] == "turn_done", "the cap must still CLOSE the turn"
    assert events[-1]["content"] == ""
    assert conversation.messages[-1].role == "tool"  # last tool result recorded


async def test_injection_processed_next_iteration(workspace):
    provider = MockProvider(
        [
            MockTurn(tool_calls=[MockToolCall("list_dir", {})]),
            MockTurn(content_chunks=["done"]),
        ]
    )
    loop, conversation = build(workspace, provider)
    loop.injections.append("focus on py files")
    events: list[dict] = []
    await loop.run("start", events.append)
    injected = [m for m in conversation.messages if m.content and "mid-stream" in m.content]
    assert injected and "focus on py files" in injected[0].content


async def test_interrupt_leaves_wire_valid(workspace):
    class SlowProvider(MockProvider):
        async def stream(self, messages, tools):
            async for event in super().stream(messages, tools):
                await asyncio.sleep(0.05)
                yield event

    provider = SlowProvider([MockTurn(content_chunks=["chunk"] * 100, finish_reason="stop")])
    loop, conversation = build(workspace, provider)
    events: list[dict] = []

    async def run():
        await loop.run("hi", events.append)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.15)  # let a few deltas through
    task.cancel()
    try:
        await task
        cancelled = False
    except asyncio.CancelledError:
        cancelled = True
    assert cancelled
    interrupted_events = [e for e in events if e["type"] == "interrupted"]
    assert interrupted_events
    # partial assistant content preserved and wire format stays valid
    partial = [m for m in conversation.messages if m.partial]
    assert partial and partial[0].content
    wire = conversation.wire_messages()
    assert wire[-1]["role"] in ("assistant", "user", "tool")


async def test_non_retryable_error_emits_and_raises(workspace):
    class FailingProvider:
        model_id = "mock/fail"

        async def stream(self, messages, tools):
            raise ProviderError("auth", "bad key", retryable=False)
            yield  # pragma: no cover

    loop, _ = build(workspace, FailingProvider())
    events: list[dict] = []
    try:
        await loop.run("hi", events.append)
        raised = False
    except ProviderError:
        raised = True
    assert raised
    errors = [e for e in events if e["type"] == "error"]
    assert errors and errors[0]["hint"]  # hint attached for the UI


async def test_on_message_persistence_hook(workspace):
    provider = MockProvider([MockTurn(content_chunks=["ok"])])
    loop, conversation = build(workspace, provider)
    persisted: list[str] = []
    loop.on_message = lambda m: persisted.append(m.role)
    await loop.run("hi", lambda e: None)
    assert persisted == ["user", "assistant"]


async def test_arguments_summary():
    from oaset.agent.loop import arguments_summary

    assert arguments_summary('{"path": "a.txt", "n": 3}') == "path=a.txt, n=3"
    assert arguments_summary("not json") == "not json"
    assert arguments_summary("") == "{}" or arguments_summary("") == ""
    nested = arguments_summary(
        '{"todos": [{"content": "用 GitHub API 拉取 topic:cli", "status": "in_progress"}]}')
    assert "todos=[1]" in nested
    assert "GitHub API" not in nested


# ------------------------------------------- retry partials: replace, not append

class _FlakyThenGood:
    """attempt 1 dies mid-stream, attempt 2 regenerates the WHOLE answer."""

    model_id = "mock/flaky"

    def __init__(self) -> None:
        self.attempt = 0

    async def stream(self, messages, tools):
        from oaset.providers.base import ContentDelta, ReasoningDelta, StreamDone

        self.attempt += 1
        if self.attempt == 1:
            yield ContentDelta(text="ABC")
            raise ProviderError("server", "mid-stream", retryable=True)
        yield ContentDelta(text="ABCDEF")
        yield ReasoningDelta(text="thinking…")
        yield StreamDone(finish_reason="stop", usage=None)


async def test_retry_replaces_the_dead_attempts_prefix(workspace, monkeypatch):
    """Providers restart the answer on retry — appending the preserved
    prefix duplicated everything the failed attempt had already streamed."""
    import oaset.agent.loop as loop_mod

    monkeypatch.setattr(loop_mod, "RETRY_BACKOFF", [0.01])
    loop, conversation = build(workspace, _FlakyThenGood())
    events: list[dict] = []
    await loop.run("hi", events.append)
    final = [m for m in conversation.messages if m.role == "assistant"][-1]
    assert final.content == "ABCDEF", "prefix must not be duplicated on retry"
    # exactly ONE preservation notice, and both locales render the held
    # length (3) in it — a second one would mean the prefix got re-held
    held = [e for e in events if e["type"] == "notice"
            and "3" in str(e.get("text", ""))]
    assert len(held) == 1


class _DiesAfterPartial:
    model_id = "mock/dead"

    def __init__(self, retryable: bool) -> None:
        self.retryable = retryable

    async def stream(self, messages, tools):
        from oaset.providers.base import ContentDelta, ReasoningDelta

        yield ContentDelta(text="PART")
        yield ReasoningDelta(text="secret reasoning")
        raise ProviderError("server", "dead", retryable=self.retryable)


async def test_terminal_partial_keeps_reasoning_out_of_content(workspace):
    """The no-fallback terminal path used to concatenate reasoning INTO
    content (`preserved + preserved_reasoning`) — the user saw the chain
    of thought twice, once as content."""
    loop, conversation = build(workspace, _DiesAfterPartial(retryable=False))
    events: list[dict] = []
    await loop.run("hi", events.append)
    partial = [m for m in conversation.messages if m.partial][-1]
    assert partial.content == "PART"
    assert (partial.reasoning or "") == "secret reasoning"


async def test_fallback_carries_the_partial_as_its_floor(workspace, monkeypatch):
    """The fallback recursion used to reset `preserved` to empty — a
    fallback that died before emitting anything lost the earlier partial."""
    import oaset.agent.loop as loop_mod

    monkeypatch.setattr(loop_mod, "RETRY_BACKOFF", [0.01])

    class _InstantDead:
        model_id = "mock/fb"

        async def stream(self, messages, tools):
            raise ProviderError("auth", "no key", retryable=False)
            yield  # pragma: no cover

    loop, conversation = build(workspace, _DiesAfterPartial(retryable=True))
    loop.fallbacks = [_InstantDead()]
    events: list[dict] = []
    await loop.run("hi", events.append)
    partial = [m for m in conversation.messages if m.partial][-1]
    assert partial.content == "PART"


# -------------------------------- retry-window Esc keeps the streamed part

async def test_cancel_during_retry_backoff_keeps_the_held_partial(workspace, monkeypatch):
    """Esc inside the 1/2/4s backoff sleep (or before the next attempt's
    first delta) used to drop everything the user had already watched
    stream: the CancelledError never reached the handler and `preserved`
    died with the local."""
    import contextlib

    import oaset.agent.loop as loop_mod

    monkeypatch.setattr(loop_mod, "RETRY_BACKOFF", [5])

    class FlakyThenBackoff(MockProvider):
        def __init__(self, turns):
            super().__init__(turns)
            self.attempt = 0

        async def stream(self, messages, tools):
            self.attempt += 1
            if self.attempt == 1:
                yield __import__("oaset.providers.base", fromlist=["b"]).ContentDelta(text="ABC")
                raise ProviderError("server", "mid-stream", retryable=True)
            async for event in super().stream(messages, tools):
                yield event

    loop, conversation = build(workspace, FlakyThenBackoff(
        [MockTurn(content_chunks=["chunk"] * 50)]))
    events: list[dict] = []

    async def run():
        await loop.run("hi", events.append)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.4)  # first attempt died, now inside the 5s backoff
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    partial = [m for m in conversation.messages if m.partial]
    assert partial and partial[0].content == "ABC", (
        "what the user watched stream must land in the transcript")


async def test_carried_edits_do_not_trip_requirements_on_a_planning_turn(workspace):
    """The requirements-completeness gate keys on THIS turn's work: a
    pure-planning follow-up after an interrupted edit turn may end with a
    pending plan — carried paths alone must not inject the [requirements]
    nudge (the verify-gate naming them is intended and separate)."""
    import contextlib

    (workspace / "v.py").write_text("value = 1\n", encoding="utf-8")

    class SlowThenDie(MockProvider):
        def __init__(self):
            super().__init__([
                MockTurn(tool_calls=[MockToolCall("edit_file", {
                    "path": "v.py", "old_text": "value = 1",
                    "new_text": "value = 2"})]),
                MockTurn(content_chunks=["chunk"] * 100),
            ])

        async def stream(self, messages, tools):
            async for event in super().stream(messages, tools):
                await asyncio.sleep(0.05)
                yield event

    loop, conversation = build(workspace, SlowThenDie())
    state = loop.registry.ctx.session_state
    state["todos"] = [{"content": "later work", "status": "in_progress"}]
    events: list[dict] = []

    async def run():
        await loop.run("fix it", events.append)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.6)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert state.get("verify_pending_paths") == ["v.py"]

    plain = MockProvider([
        MockTurn(content_chunks=["plan: do it tomorrow"]),
        MockTurn(content_chunks=["final"]),
    ])
    loop2 = AgentLoop(plain, loop.registry, conversation)
    events2: list[dict] = []
    await loop2.run("what's the plan?", events2.append)
    nudges = [str(m.content) for m in conversation.messages
              if m.role == "user" and m.content]
    assert any("[verify-gate]" in text for text in nudges), (
        "the carried edit is still named — that part is intended")
    assert not any("[requirements]" in text for text in nudges), (
        "a planning-only turn must not carry the requirements add-on")


async def test_live_view_resets_the_dead_prefix_on_retry(workspace, monkeypatch):
    """The retry REPLACE semantics must reach the delta channel too: the
    TUI hides the dead attempt's streamed text when the retry notice
    arrives, so the live view converges to the persisted message."""
    import oaset.agent.loop as loop_mod
    from oaset.config import default_config
    from oaset.providers.base import ContentDelta, StreamDone
    from oaset.tui.app import OasetApp

    monkeypatch.setattr(loop_mod, "RETRY_BACKOFF", [0.01])

    class RetryThenRegenerate(MockProvider):
        def __init__(self):
            super().__init__([MockTurn(content_chunks=["unused"])])
            self.attempt = 0

        async def stream(self, messages, tools):
            self.attempt += 1
            if self.attempt == 1:
                yield ContentDelta(text="ABC")
                raise ProviderError("server", "mid-stream", retryable=True)
            yield ContentDelta(text="ABCDEF")
            yield StreamDone(finish_reason="stop", usage=None)

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=RetryThenRegenerate(),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(120, 40)) as pilot:
        app.submit_text("hi")
        for _ in range(60):
            await pilot.pause(0.1)
            if app.chat.transcript_text().count("ABCDEF"):
                break
        text = app.chat.transcript_text()
        assert "ABCABCDEF" not in text, "the dead prefix must not double"
        assert "ABCDEF" in text
