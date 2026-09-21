"""Thinking-effort adaptation: one dial (off/light/medium/heavy) mapped to
each provider's real knob — Anthropic budget (with max_tokens/temperature
rules), OpenAI reasoning_effort, Gemini thinkingBudget, GLM/Qwen toggles,
and an honest "no knob" for model-picked thinking (DeepSeek/Kimi).

Hermetic: pure mapping tests + MockTransport wire captures + a fake
provider for the loop sync.
"""

from __future__ import annotations

import json

import httpx
import pytest

from oaset.thinking import (
    LEVELS,
    apply_thinking,
    describe,
    normalize_level,
    thinking_params,
)

# ------------------------------------------------------------- normalizing

def test_normalize_level_accepts_aliases_and_rejects_noise():
    assert normalize_level("LOW") == "light"
    assert normalize_level("minimal") == "light"
    assert normalize_level("High") == "heavy"
    assert normalize_level("  max ") == "heavy"
    assert normalize_level("off") == "off"
    assert normalize_level("") == "" and normalize_level(None) == ""
    assert normalize_level("sideways") == "", "unknown text must not guess"
    assert LEVELS == ("off", "light", "medium", "heavy")


# --------------------------------------------------------------- mappings

def test_anthropic_budgets_and_off_marker():
    heavy = thinking_params("anthropic/claude-sonnet", "heavy", "anthropic")
    assert heavy == {"thinking": {"type": "enabled", "budget_tokens": 24576}}
    assert thinking_params("anthropic/claude-sonnet", "light", "anthropic")[
        "thinking"]["budget_tokens"] >= 1024, "Anthropic budget floor"
    # off is a DELETE marker, never a wire value
    assert thinking_params("anthropic/claude-sonnet", "off", "anthropic") == {
        "thinking": None}


def test_openai_reasoning_effort_and_gpt5_minimal():
    assert thinking_params("openai/gpt-5.1", "heavy", "openai") == {
        "reasoning_effort": "high"}
    assert thinking_params("openai/gpt-5.1", "off", "openai") == {
        "reasoning_effort": "minimal"}, "gpt-5 has a near-off level"
    assert thinking_params("openai/o4-mini", "off", "openai") == {}, (
        "o-series has no off — send nothing instead of lying")
    assert thinking_params("azure/gpt-5", "light", "azure") == {
        "reasoning_effort": "low"}


def test_chinese_openai_compat_toggles():
    assert thinking_params("glm/glm-5.3", "medium", "openai") == {
        "thinking": {"type": "enabled"}}
    assert thinking_params("glm/glm-5.3", "off", "openai") == {
        "thinking": {"type": "disabled"}}
    assert thinking_params("qwen/qwen3-coder", "heavy", "openai") == {
        "enable_thinking": True}
    assert thinking_params("qwen/qwen3-coder", "off", "openai") == {
        "enable_thinking": False}


def test_gemini_budgets_pro_floor_and_gemini3_level():
    flash = thinking_params("gemini/gemini-2.5-flash", "off", "gemini")
    assert flash["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0
    pro = thinking_params("gemini/gemini-2.5-pro", "off", "gemini")
    assert pro["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 128, (
        "2.5 Pro cannot disable thinking; floor instead of a rejected request")
    g3 = thinking_params("gemini/gemini-3-pro", "heavy", "gemini")
    assert g3["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}


def test_model_picked_thinking_has_no_knob():
    assert thinking_params("deepseek/deepseek-reasoner", "heavy", "openai") == {}
    assert thinking_params("moonshot/kimi-k2", "off", "openai") == {}
    assert thinking_params("whatever/x", "medium", "openai") == {}
    assert describe("deepseek/deepseek-reasoner", "openai")[0] == "none"


def test_describe_kinds_for_display():
    assert describe("anthropic/claude-sonnet", "anthropic") == (
        "budget", "thinking.budget_tokens")
    assert describe("openai/gpt-5", "openai")[0] == "effort"
    assert describe("gemini/gemini-flash", "gemini")[0] == "gemini_budget"
    assert describe("glm/glm-5.3", "openai")[0] == "toggle"
    assert describe("qwen/qwen3", "openai")[0] == "qwen_toggle"


# ------------------------------------------------------------ apply_rules

def test_apply_thinking_raises_max_tokens_above_budget():
    payload = {"max_tokens": 8192}
    apply_thinking(payload, "anthropic/claude-sonnet", "heavy", "anthropic")
    assert payload["thinking"]["budget_tokens"] == 24576
    assert payload["max_tokens"] > 24576, (
        "Anthropic rejects budget >= max_tokens; the default 8192 would 400")


def test_apply_thinking_off_deletes_capability_default():
    payload = {"max_tokens": 8192,
               "thinking": {"type": "enabled", "budget_tokens": 5000}}
    apply_thinking(payload, "anthropic/claude-sonnet", "off", "anthropic")
    assert "thinking" not in payload


def test_apply_thinking_merges_nested_gemini_config():
    payload = {"generationConfig": {"thinkingConfig": {"includeThoughts": True}}}
    apply_thinking(payload, "gemini/gemini-2.5-flash", "medium", "gemini")
    cfg = payload["generationConfig"]["thinkingConfig"]
    assert cfg["thinkingBudget"] == 8192 and cfg["includeThoughts"] is True


def test_apply_thinking_blank_level_is_a_no_op():
    payload = {"max_tokens": 8192}
    apply_thinking(payload, "anthropic/claude-sonnet", "", "anthropic")
    assert payload == {"max_tokens": 8192}


# ------------------------------------------------- provider wire integration

def _capture_client(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.read()))
        if "generativelanguage" in str(request.url):
            events = [{"candidates": [{"content": {"parts": [{"text": "ok"}]},
                                       "finishReason": "STOP"}]},
                      {"usageMetadata": {"promptTokenCount": 5,
                                         "candidatesTokenCount": 1,
                                         "totalTokenCount": 6}}]
            body = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()
            return httpx.Response(200, content=body,
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, content=b"", headers={"content-type": "text/event-stream"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_anthropic_wire_gets_budget_temperature_and_max_tokens():
    from oaset.providers.anthropic_native import AnthropicNativeProvider

    captured: dict = {}
    provider = AnthropicNativeProvider(
        "anthropic/claude-sonnet", api_key="k",
        client=_capture_client(captured), prompt_cache=False)
    provider.thinking_level = "heavy"
    async for _ in provider.stream([{"role": "user", "content": "hi"}], None):
        pass
    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 24576}
    assert captured["temperature"] == 1, "thinking requires temperature=1"
    assert captured["max_tokens"] > 24576


@pytest.mark.asyncio
async def test_gemini_wire_gets_thinking_budget():
    from oaset.providers.gemini_native import GeminiNativeProvider

    captured: dict = {}
    provider = GeminiNativeProvider("gemini/gemini-2.5-flash", api_key="KEY",
                                    client=_capture_client(captured))
    provider.thinking_level = "light"
    async for _ in provider.stream([{"role": "user", "content": "hi"}], None):
        pass
    cfg = captured["generationConfig"]["thinkingConfig"]
    assert cfg["thinkingBudget"] == 2048


@pytest.mark.asyncio
async def test_loop_syncs_session_level_onto_active_provider():
    """The agent loop copies session_state.thinking_level onto the ACTIVE
    provider before every call — including after a fallback swap."""
    from oaset.agent import AgentLoop, Conversation

    class RecordingProvider:
        def __init__(self) -> None:
            self.thinking_level = ""
            self.calls = 0

        async def stream(self, messages, tools):
            self.calls += 1
            from oaset.providers.base import StreamDone

            yield StreamDone(finish_reason="stop",
                             usage={"prompt_tokens": 1, "completion_tokens": 1,
                                    "total_tokens": 2})

    from oaset.tools import ToolRegistry
    from oaset.tools.base import AutoGate, ToolContext

    provider = RecordingProvider()
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=_tmp_ws(), mode="auto", output_limit=200))
    registry.ctx.session_state["thinking_level"] = "medium"
    loop = AgentLoop(provider, registry, Conversation(system_prompt="s"),
                     max_iterations=2)
    events: list[dict] = []
    await loop.run("hello", events.append)
    assert provider.thinking_level == "medium"


def _tmp_ws():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp())


# ------------------------------------------------------------- TUI command

@pytest.mark.asyncio
async def test_think_command_sets_level_and_warns_honestly(workspace):
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn
    from oaset.tui.app import OasetApp

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        state = app.registry.ctx.session_state
        # no level anywhere yet → no badge in the status bar
        assert "✦" not in (app.status_bar.left_text or "")
        await app.cmd_think("heavy")
        await pilot.pause(0.05)
        assert state["thinking_level"] == "heavy"
        assert state["thinking_level_set"] is True
        assert "heavy" in (app.status_bar.left_text or ""), (
            "the dial must be visible where the user looks between turns")

        # mock has no knob: the honest no-knob notice must appear
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "mock" in rendered.lower() or "none" in rendered.lower() or (
            "无" in rendered)

        await app.cmd_think("bananas")
        await pilot.pause(0.05)
        assert state["thinking_level"] == "heavy", "unknown level changes nothing"

        await app.cmd_think("")
        await pilot.pause(0.05)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "heavy" in rendered, "status card shows the current level"


# ----------------------------------------------------- CLI --think full chain

@pytest.mark.asyncio
async def test_cli_think_flag_reaches_the_provider(workspace):
    """--think → run_one_shot seeds session_state → the loop syncs it onto
    the provider before the first model call."""
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn

    class LevelRecording(MockProvider):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.thinking_level = "untouched"

    from oaset.cli import run_one_shot

    provider = LevelRecording([MockTurn(content_chunks=["ok"])])
    rc = await run_one_shot(
        default_config(), workspace, "hello", "mock/mock-echo", provider,
        yolo=True, thinking="heavy")
    assert rc == 0
    assert provider.thinking_level == "heavy"


# --------------------------------------------------------- config plumbing

def test_model_thinking_field_roundtrips(cfg):
    from oaset.config import load_config, save_config

    cfg.models["mock/mock-echo"].thinking = "medium"
    save_config(cfg)
    loaded = load_config()
    assert loaded.models["mock/mock-echo"].thinking == "medium"


# ------------------------------------------- per-agent thinking dial

def test_agent_header_thinking_normalizes(workspace):
    from pathlib import Path

    from oaset.agents import find_agent

    agents_dir = workspace / ".oaset" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "scraper.md").write_text(
        "name: Scraper\ndescription: mechanical grep work\n"
        "thinking: HIGH\nmax_iterations: 3\n\nYou grep.\n", encoding="utf-8")
    (agents_dir / "sloppy.md").write_text(
        "name: Sloppy\ndescription: bad header value\n"
        "thinking: sideways\n\nYou drift.\n", encoding="utf-8")
    assert find_agent(Path(workspace), "scraper").thinking == "heavy", (
        "header aliases normalize (HIGH → heavy)")
    assert find_agent(Path(workspace), "sloppy").thinking == "", (
        "unknown values fall back to inherit, never guess")


@pytest.mark.asyncio
async def test_subagent_runs_at_its_own_dial_not_the_parents(workspace, isolated_home):
    """A per-agent thinking level must reach the provider the sub-loop
    drives — the parent's level must not override it, and after the task
    the parent's own pre-call sync restores its level."""
    from oaset.agent.subagent_runner import make_subagent_runner
    from oaset.providers import MockProvider, MockTurn
    from oaset.tools import AutoGate, ToolContext, ToolRegistry

    class LevelRecording(MockProvider):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.seen_levels: list[str] = []

        async def stream(self, messages, tools):
            self.seen_levels.append(self.thinking_level)
            async for event in super().stream(messages, tools):
                yield event

    agents_dir = workspace / ".oaset" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "grepper.md").write_text(
        "name: grepper\ndescription: mechanical search\nthinking: off\n\n"
        "You grep and report.\n", encoding="utf-8")

    provider = LevelRecording([MockTurn(content_chunks=["sub answer"])])
    provider.thinking_level = "heavy"  # the parent's current dial
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=800))
    registry.ctx.session_state["thinking_level"] = "heavy"
    runner = make_subagent_runner(provider, registry, registry.ctx,
                                  max_iterations=4, context_max_tokens=32000,
                                  config=None)
    from pathlib import Path as _P

    from oaset.agents import find_agent

    grepper = find_agent(_P(workspace), "grepper")
    assert grepper is not None and grepper.thinking == "off"
    report = await runner(grepper, "find the todo markers")
    assert "sub answer" in report
    assert provider.seen_levels and set(provider.seen_levels) == {"off"}, (
        "the sub-agent's own dial wins inside its turn")


# ------------------------- blind-review A-line: protocol correctness

def test_gemini_off_is_not_self_contradictory():
    off = thinking_params("gemini/gemini-2.5-flash", "off", "gemini")
    cfg = off["generationConfig"]["thinkingConfig"]
    assert cfg["thinkingBudget"] == 0
    assert cfg["includeThoughts"] is False, (
        "includeThoughts=True with a zero budget contradicts itself; some "
        "endpoints reject the pair outright")


def test_providers_gate_on_the_thinking_capability():
    from oaset.config import build_provider, default_config, resolve_model
    from oaset.providers import MockProvider  # noqa: F401

    cfg = default_config()
    # gpt-4o has no "thinking" capability: reasoning_effort would 400
    p = build_provider(cfg, resolve_model(cfg, "openai/gpt-4o"))
    assert p.thinking_supported is False
    # claude-sonnet does: the parameter is legal
    p = build_provider(cfg, resolve_model(cfg, "anthropic/claude-sonnet"))
    assert p.thinking_supported is True


@pytest.mark.asyncio
async def test_unsupported_model_never_receives_the_thinking_param():
    """gpt-4o (no thinking capability) 400s on reasoning_effort — the
    provider must drop the dial instead of bricking every request."""
    from oaset.providers.openai_compat import OpenAICompatProvider

    class _Completions:
        def __init__(self, sink):
            self._sink = sink

        async def create(self, **kwargs):
            self._sink.update(kwargs)

            class _Stream:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    raise StopAsyncIteration

            return _Stream()

    class _Chat:
        def __init__(self, sink):
            self.completions = _Completions(sink)

    class _Client:
        def __init__(self, sink):
            self.chat = _Chat(sink)
            self.base_url = "https://api.example.com/v1"

        async def aclose(self):
            pass

    captured: dict = {}
    provider = OpenAICompatProvider(
        "openai/gpt-4o", api_key="k", base_url="https://api.example.com/v1",
        client=_Client(captured))
    provider.thinking_supported = False
    provider.thinking_level = "heavy"
    async for _ in provider.stream([{"role": "user", "content": "hi"}], None):
        pass
    assert "reasoning_effort" not in captured
    # and the openai wire never sees the thinking-replay extras either
    assert all("reasoning" not in m and "signature" not in m
               for m in captured["messages"])


# ---------------- signature capture, replay, persistence (A1/A3)

@pytest.mark.asyncio
async def test_anthropic_signature_round_trips_into_the_replay_block():
    """Interleaved thinking: the thinking block + signature of the LAST
    assistant turn must be replayed with its tool_use blocks — without it
    every follow-up request in a thinking-enabled tool loop was a 400."""
    from oaset.providers.anthropic_native import AnthropicNativeProvider
    from oaset.providers.base import ThinkingSignature

    sse = (
        'data: {"type":"message_start","message":{"usage":{"input_tokens":10}}}\n\n'
        'data: {"type":"content_block_delta","delta":{"type":"thinking_delta",'
        '"thinking":"let me think"}}\n\n'
        'data: {"type":"content_block_delta","delta":{"type":"signature_delta",'
        '"signature":"sig-abc"}}\n\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta",'
        '"text":"calling"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\n'
        'data: [DONE]\n\n'
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, content=sse.encode(),
                              headers={"content-type": "text/event-stream"})

    provider = AnthropicNativeProvider(
        "anthropic/claude-sonnet", api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    got_sig = ""
    async for event in provider.stream([{"role": "user", "content": "hi"}], None):
        if isinstance(event, ThinkingSignature):
            got_sig = event.text
    assert got_sig == "sig-abc", "the signature must be captured, not dropped"

    # second call replays it with the assistant turn that owns the tools
    # (thinking ON for that request — the replay gates on it)
    provider.thinking_level = "heavy"
    wire = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "calling", "reasoning": "let me think",
         "signature": "sig-abc",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "read_file",
                                      "arguments": "{\"path\":\"a\"}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "data"},
    ]
    async for _ in provider.stream(wire, None):
        pass
    last_assistant = [m for m in captured["body"]["messages"]
                      if m["role"] == "assistant"][-1]
    head = last_assistant["content"][0]
    assert head["type"] == "thinking" and head["signature"] == "sig-abc"
    assert head["thinking"] == "let me think"
    assert last_assistant["content"][-1]["type"] == "tool_use"


def test_thinking_signature_and_call_signatures_persist():
    from oaset.agent.messages import Conversation, Message, ToolCallReq
    from oaset.agent.messages import Conversation as _C

    conv = Conversation("sys")
    conv.append(Message(
        role="assistant", content="calling", reasoning="think",
        thinking_signature="sig-1",
        tool_calls=[ToolCallReq(id="g1", name="read_file", arguments="{}",
                                signature="g-sig")]))
    restored = _C.from_dicts(conv.to_dicts())
    m = restored.messages[0]
    assert m.thinking_signature == "sig-1" and m.reasoning == "think"
    assert m.tool_calls[0].signature == "g-sig"
    wire = m.to_wire()
    assert wire["signature"] == "sig-1" and wire["reasoning"] == "think"
    assert wire["tool_calls"][0]["signature"] == "g-sig"


@pytest.mark.asyncio
async def test_gemini_captures_and_replays_thought_signature():
    from oaset.providers.base import ToolCallEmitted
    from oaset.providers.gemini_native import GeminiNativeProvider, _to_gemini

    events = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "read_file", "args": {"path": "a"}},
             "thoughtSignature": "g3-sig"},
        ]}, "finishReason": "STOP"}]},
        {"usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1,
                           "totalTokenCount": 6}},
    ]
    sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse,
                              headers={"content-type": "text/event-stream"})

    provider = GeminiNativeProvider("gemini/gemini-3-pro", api_key="KEY",
                                    client=httpx.AsyncClient(
                                        transport=httpx.MockTransport(handler)))
    got: dict = {}
    async for event in provider.stream([{"role": "user", "content": "hi"}], None):
        if isinstance(event, ToolCallEmitted):
            got["signature"] = event.signature
    assert got["signature"] == "g3-sig", "thoughtSignature must not be dropped"

    # replay: the wire dict with a per-call signature round-trips
    _sys, contents = _to_gemini([
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "gem_0", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"},
                         "signature": "g3-sig"}]},
    ])
    fc_part = [p for p in contents[-1]["parts"] if "functionCall" in p][0]
    assert fc_part["thoughtSignature"] == "g3-sig"


@pytest.mark.asyncio
async def test_tui_launch_flag_seeds_the_level_after_host_ready(workspace):
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn
    from oaset.tui.app import OasetApp

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                   model_id="mock/mock-echo", thinking_level="HEAVY")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.2)
        state = app.registry.ctx.session_state
        assert state["thinking_level"] == "heavy", "alias-normalized at launch"
        assert "heavy" in (app.status_bar.left_text or "")


# ---------------- verification-round fixes (F1-F5)

@pytest.mark.asyncio
async def test_bedrock_captures_thinking_blocks_from_invoke():
    """The replay wiring was dead code until the response parser learned
    the thinking block: content held {type: thinking, signature} and the
    stream only ever emitted text/tool_use."""

    from oaset.providers.base import ReasoningDelta, ThinkingSignature
    from oaset.providers.bedrock_native import BedrockNativeProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "content": [
                {"type": "thinking", "thinking": "pondering",
                 "signature": "sig-b1"},
                {"type": "text", "text": "answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        })

    provider = BedrockNativeProvider(
        "bedrock/claude", access_key="AK", secret_key="s",
        region="us-east-1", client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
    kinds: dict[str, str] = {}
    async for event in provider.stream([{"role": "user", "content": "hi"}], None):
        if isinstance(event, ReasoningDelta):
            kinds["reasoning"] = event.text
        if isinstance(event, ThinkingSignature):
            kinds["signature"] = event.text
    assert kinds["reasoning"] == "pondering" and kinds["signature"] == "sig-b1"


@pytest.mark.asyncio
async def test_history_thinking_blocks_strip_when_thinking_is_off():
    """A session that once thought, then /think off (or a model without the
    capability), used to send replayed thinking blocks to a request with
    thinking disabled — a 400 by the API's own rule."""
    from oaset.providers.anthropic_native import AnthropicNativeProvider

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, content=b"",
                              headers={"content-type": "text/event-stream"})

    wire = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "call", "reasoning": "thought",
         "signature": "sig-1",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "read_file",
                                      "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "data"},
    ]
    provider = AnthropicNativeProvider(
        "anthropic/claude-sonnet", api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    provider.thinking_level = ""  # thinking off for this request
    async for _ in provider.stream(wire, None):
        pass
    blocks = [b for m in captured["body"]["messages"]
              if isinstance(m.get("content"), list)
              for b in m["content"]]
    assert not any(b.get("type") in ("thinking", "redacted_thinking")
                   for b in blocks), "replayed thinking must strip when off"


@pytest.mark.asyncio
async def test_redacted_thinking_replays_verbatim():
    from oaset.providers.anthropic_native import AnthropicNativeProvider
    from oaset.providers.base import ThinkingRedacted

    captured: dict = {}
    sse = (
        'data: {"type":"content_block_start","content_block":'
        '{"type":"redacted_thinking","data":"blob"}}\n\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta",'
        '"text":"ok"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'data: [DONE]\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, content=sse.encode(),
                              headers={"content-type": "text/event-stream"})

    provider = AnthropicNativeProvider(
        "anthropic/claude-sonnet", api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    saw_redacted = False
    async for event in provider.stream([{"role": "user", "content": "hi"}], None):
        if isinstance(event, ThinkingRedacted):
            saw_redacted = True
    assert saw_redacted

    provider.thinking_level = "heavy"
    wire = [
        {"role": "assistant", "content": "call", "reasoning": "blob",
         "redacted_thinking": True,
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "read_file",
                                      "arguments": "{}"}}]},
    ]
    async for _ in provider.stream(wire, None):
        pass
    block = captured["body"]["messages"][-1]["content"][0]
    assert block == {"type": "redacted_thinking", "data": "blob"}, (
        "redacted blocks replay verbatim, never as signed thinking")


def test_overcounted_hunk_does_not_swallow_the_next_file_header():
    """Models over-count hunk lines; the counting alone then ate the next
    file's real ---/+++ pair. The escape hatch trusts a path-shaped pair
    followed by @@ over the counts."""
    from oaset.tools.patch import parse_unified_diff

    diff = (
        "--- a/one.py\n"
        "+++ b/one.py\n"
        "@@ -1,10 +1,10 @@\n"   # claims 10; delivers 2
        " a\n"
        " b\n"
        "--- a/two.py\n"
        "+++ b/two.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    files = parse_unified_diff(diff)
    assert [f[0] for f in files] == ["one.py", "two.py"], (
        "an over-counted hunk must not swallow the next file")


@pytest.mark.asyncio
async def test_live_reset_clears_the_dead_attempts_reasoning(workspace, monkeypatch):
    """reset_stream left the thinking buffer full of the dead attempt's
    reasoning — copy/expand carried stale thought while the persisted
    message kept only the winner's."""
    import oaset.agent.loop as loop_mod
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn
    from oaset.providers.base import ContentDelta, ReasoningDelta, StreamDone
    from oaset.tui.app import OasetApp

    monkeypatch.setattr(loop_mod, "RETRY_BACKOFF", [0.01])

    class ThinkyRetry(MockProvider):
        def __init__(self):
            super().__init__([MockTurn(content_chunks=["unused"])])
            self.attempt = 0

        async def stream(self, messages, tools):
            self.attempt += 1
            if self.attempt == 1:
                yield ReasoningDelta(text="DEAD-THOUGHT")
                from oaset.providers.base import ProviderError as _PE

                raise _PE("server", "mid", retryable=True)
            yield ReasoningDelta(text="LIVE-THOUGHT")
            yield ContentDelta(text="final answer")
            yield StreamDone(finish_reason="stop", usage=None)

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=ThinkyRetry(), model_id="mock/mock-echo")
    async with app.run_test(size=(120, 40)) as pilot:
        app.submit_text("hi")
        for _ in range(60):
            await pilot.pause(0.1)
            if "final answer" in app.chat.transcript_text():
                break
        from oaset.tui.widgets.chat import AssistantTurn

        turns = list(app.chat.query(AssistantTurn))
        assert turns, "the turn segment must exist"
        seg = turns[-1]
        thought = "".join(seg.thinking._buffer)
        assert "DEAD-THOUGHT" not in thought, "stale reasoning must reset"
        assert "LIVE-THOUGHT" in thought


# ---------------- re-review round (G1-G3)

@pytest.mark.asyncio
async def test_bedrock_redacted_replays_verbatim():
    """F3's bedrock half: a redacted turn's replay must be the verbatim
    data block — the signed-only branch silently replayed nothing."""
    import json as _json  # noqa: F401

    from oaset.providers.bedrock_native import BedrockNativeProvider

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"content": [], "stop_reason": "end_turn",
                                         "usage": {}})

    provider = BedrockNativeProvider(
        "bedrock/claude", access_key="AK", secret_key="s",
        region="us-east-1", client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)),
        prompt_cache=False)
    provider.thinking_level = "heavy"
    wire = [
        {"role": "assistant", "content": "call", "reasoning": "blob",
         "redacted_thinking": True,
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "read_file",
                                      "arguments": "{}"}}]},
    ]
    async for _ in provider.stream(wire, None):
        pass
    block = captured["body"]["messages"][-1]["content"][0]
    assert block == {"type": "redacted_thinking", "data": "blob"}


@pytest.mark.asyncio
async def test_bedrock_accumulates_multiple_thinking_blocks():
    """Several thinking blocks in one response must not silently keep only
    the last one."""
    from oaset.providers.base import ReasoningDelta, ThinkingSignature
    from oaset.providers.bedrock_native import BedrockNativeProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "content": [
                {"type": "thinking", "thinking": "first-", "signature": "s1"},
                {"type": "thinking", "thinking": "second", "signature": "s2"},
            ],
            "stop_reason": "end_turn", "usage": {}})

    provider = BedrockNativeProvider(
        "bedrock/claude", access_key="AK", secret_key="s",
        region="us-east-1", client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
    got: dict = {}
    async for event in provider.stream([{"role": "user", "content": "hi"}], None):
        if isinstance(event, ReasoningDelta):
            got["text"] = got.get("text", "") + event.text
        if isinstance(event, ThinkingSignature):
            got["sig"] = got.get("sig", "") + event.text
    assert got["text"] == "first-second" and got["sig"] == "s1s2"


def test_overcounted_hunk_with_spaced_path_still_splits_files():
    """The escape hatch must not go blind on spaced filenames (or git's
    quoted forms) — content pairs like `-- old setting` stay content via
    the leading -/+ test; `my file.py` is a path."""
    from oaset.tools.patch import parse_unified_diff

    diff = (
        "--- a/one.py\n"
        "+++ b/one.py\n"
        "@@ -1,10 +1,10 @@\n"
        " a\n"
        " b\n"
        '--- "a/my file.py"\n'
        '+++ "b/my file.py"\n'
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    files = parse_unified_diff(diff)
    assert [f[0] for f in files] == ["one.py", "my file.py"]
