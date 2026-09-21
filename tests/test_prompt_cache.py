"""Prompt caching: anthropic breakpoints + real usage accounting, /context hits.

Hermetic by construction: the wire is a MockTransport capture, usage lines are
canned objects, and the store reads a temp transcript.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from oaset.providers.anthropic_native import AnthropicNativeProvider
from oaset.providers.openai_compat import _usage_dict


def _anthropic_provider(body_callback, prompt_cache: bool):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        captured["beta"] = request.headers.get("anthropic-beta", "")
        body_callback(captured)
        return httpx.Response(200, content=b"", headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicNativeProvider("anthropic/claude-sonnet", api_key="sk",
                                       client=client, prompt_cache=prompt_cache)
    return provider, captured


def _wire_messages():
    return [
        {"role": "system", "content": "you are a coding agent"},
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "read_file",
                          "arguments": json.dumps({"path": "a.py"})}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": "x = 1"},
    ]


@pytest.mark.asyncio
async def test_anthropic_cache_marks_system_tools_and_last_tool_result():
    """The three breakpoints, cheapest-first stability: tool table, system
    prompt, newest tool result. Without prompt_cache, no marker may exist."""
    provider, captured = _anthropic_provider(lambda c: None, prompt_cache=True)
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}},
             {"type": "function", "function": {"name": "list_dir", "parameters": {}}}]
    async for _ in provider.stream(_wire_messages(), tools):
        pass
    body = captured["body"]

    assert isinstance(body["system"], list)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}, (
        "one breakpoint on the last tool caches the whole table")
    assert body["tools"][0].get("cache_control") is None
    last_user = body["messages"][-1]
    assert last_user["content"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_anthropic_without_cache_has_no_markers():
    provider, captured = _anthropic_provider(lambda c: None, prompt_cache=False)
    async for _ in provider.stream(_wire_messages(),
                                   [{"type": "function", "function": {"name": "read_file",
                                                                      "parameters": {}}}]):
        pass
    body = captured["body"]
    assert isinstance(body["system"], str)
    assert all("cache_control" not in t for t in body["tools"])
    assert all("cache_control" not in b
               for m in body["messages"] if isinstance(m.get("content"), list)
               for b in m["content"])


@pytest.mark.asyncio
async def test_anthropic_usage_keeps_cache_read_and_write():
    """message_start carries the cache fields; message_delta must merge over
    them instead of zeroing (it only knows output_tokens)."""
    sse = (
        'data: {"type":"message_start","message":{"usage":{"input_tokens":1000,'
        '"output_tokens":1,"cache_read_input_tokens":800,'
        '"cache_creation_input_tokens":100}}}\n\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta",'
        '"text":"ok"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":50}}\n\n'
        'data: [DONE]\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse.encode(),
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicNativeProvider("anthropic/claude-sonnet", api_key="sk",
                                       client=client, prompt_cache=True)
    done = None
    async for event in provider.stream(
            [{"role": "user", "content": "hi"}], None):
        from oaset.providers.base import StreamDone

        if isinstance(event, StreamDone):
            done = event
    assert done is not None and done.usage is not None
    assert done.usage["cache_read_tokens"] == 800
    assert done.usage["cache_write_tokens"] == 100
    assert done.usage["prompt_tokens"] == 1000
    assert done.usage["completion_tokens"] == 50, "the delta merge must not lose output"


def test_openai_usage_parses_implicit_cache_hits():
    usage = SimpleNamespace(
        prompt_tokens=1000, completion_tokens=50, total_tokens=1050,
        prompt_tokens_details=SimpleNamespace(cached_tokens=640))
    parsed = _usage_dict(usage)
    assert parsed["cache_read_tokens"] == 640
    assert parsed["prompt_tokens"] == 1000

    # providers that omit the details block keep working
    plain = _usage_dict(SimpleNamespace(prompt_tokens=10, completion_tokens=1,
                                        total_tokens=11))
    assert "cache_read_tokens" not in plain


def test_store_last_usage_reads_the_newest_line(tmp_path):
    from oaset.session import SessionStore

    store = SessionStore(home=tmp_path)
    session = store.new_session(tmp_path / "ws", model="m")
    store.append_usage(session, {"prompt_tokens": 10, "total_tokens": 12})
    store.append_usage(session, {"prompt_tokens": 500, "total_tokens": 600,
                                 "cache_read_tokens": 400, "cache_write_tokens": 80})
    last = store.last_usage(session)
    assert last["prompt_tokens"] == 500
    assert last["cache_read_tokens"] == 400 and last["cache_write_tokens"] == 80

    empty = store.new_session(tmp_path / "ws2", model="m")
    assert store.last_usage(empty) is None


@pytest.mark.asyncio
async def test_context_shows_the_cache_hit(workspace, capsys):
    """/context must surface real provider accounting when the last turn
    reported cache fields — that number is the proof the stable prefix is
    actually being reused."""
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn
    from oaset.tui.app import OasetApp

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        monkey_session = object()
        app.session = monkey_session
        hits = {"cache_read_tokens": 800, "cache_write_tokens": 100,
                "prompt_tokens": 1000}

        def fake_summary(session):
            assert session is monkey_session
            # last turn + session sums in the one payload /context reads
            return {"last": hits, "prompt_tokens": 2000,
                    "cache_read_tokens": 1500, "cache_write_tokens": 100,
                    "total_tokens": 2100, "entries": 13}

        app.store.usage_summary = fake_summary  # type: ignore[method-assign]
        app.cmd_context("")
        await pilot.pause(0.1)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "800" in rendered and "100" in rendered
        assert "80%" in rendered
        assert "13" in rendered, "session-cumulative cache line must render"


# ------------------------------------------- cross-provider cache accounting

@pytest.mark.asyncio
async def test_anthropic_cache_marks_plain_text_user_turn():
    """A turn that opens with typed input (no tool_result anywhere near the
    end) must still get the rolling breakpoint on the final user message —
    without it, every typed turn re-processes the whole conversation
    uncached and only the tools/system prefix survives."""
    provider, captured = _anthropic_provider(lambda c: None, prompt_cache=True)
    async for _ in provider.stream([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello there"},
    ], None):
        pass
    last_user = captured["body"]["messages"][-1]
    assert last_user["role"] == "user"
    assert isinstance(last_user["content"], list), "string must upgrade to blocks"
    assert last_user["content"][-1]["text"] == "hello there"
    assert last_user["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_openai_usage_parses_deepseek_style_cache_hits():
    """DeepSeek reports implicit-cache hits as top-level
    prompt_cache_hit_tokens on the OpenAI wire format."""
    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=10,
                            total_tokens=1010, prompt_cache_hit_tokens=700)
    parsed = _usage_dict(usage)
    assert parsed["cache_read_tokens"] == 700


@pytest.mark.asyncio
async def test_gemini_usage_parses_cached_content_tokens():
    """Gemini implicit caching reports the reused prefix as
    usageMetadata.cachedContentTokenCount — /context's "no cache data"
    on Gemini was a parsing gap, not an absence of caching."""
    from oaset.providers.base import StreamDone
    from oaset.providers.gemini_native import GeminiNativeProvider

    events = [
        {"candidates": [{"content": {"parts": [{"text": "ok"}]},
                         "finishReason": "STOP"}]},
        {"usageMetadata": {"promptTokenCount": 800, "candidatesTokenCount": 3,
                           "totalTokenCount": 803,
                           "cachedContentTokenCount": 512}},
    ]
    sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse,
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiNativeProvider("gemini/gemini-flash", api_key="KEY",
                                    client=client)
    done = None
    async for event in provider.stream([{"role": "user", "content": "hi"}], None):
        if isinstance(event, StreamDone):
            done = event
    assert done is not None and done.usage is not None
    assert done.usage["cache_read_tokens"] == 512
    assert done.usage["prompt_tokens"] == 800


def test_usage_summary_totals_and_last_line(tmp_path):
    from oaset.session import SessionStore

    store = SessionStore(home=tmp_path)
    session = store.new_session(tmp_path / "ws", model="m")
    store.append_usage(session, {"prompt_tokens": 1000, "total_tokens": 1010,
                                 "cache_read_tokens": 900, "cache_write_tokens": 80})
    store.append_usage(session, {"prompt_tokens": 2000, "total_tokens": 2010,
                                 "cache_read_tokens": 1800, "cache_write_tokens": 60})
    summary = store.usage_summary(session)
    assert summary["entries"] == 2
    assert summary["prompt_tokens"] == 3000
    assert summary["cache_read_tokens"] == 2700
    assert summary["cache_write_tokens"] == 140
    assert (summary["last"] or {}).get("cache_read_tokens") == 1800

    totals = store.session_usage(session.meta.session_id)
    assert totals["cache_read_tokens"] == 2700 and totals["entries"] == 2


def test_prompt_cache_defaults_on_and_optout_survives_roundtrip(cfg):
    from oaset.config import load_config, save_config

    # new default: on (rolling breakpoint wins across an agent loop)
    assert cfg.providers["mock"].prompt_cache is True
    # an explicit opt-out must round-trip, not silently re-enable
    cfg.providers["mock"].prompt_cache = False
    save_config(cfg)
    loaded = load_config()
    assert loaded.providers["mock"].prompt_cache is False


def test_prompt_v6_carries_requirement_completeness(tmp_path):
    from oaset.agent.prompts import PROMPT_VERSION, build_system_prompt

    assert PROMPT_VERSION == 7
    prompt = build_system_prompt(tmp_path)
    assert "COMPLETE THE REQUIREMENT, NOT THE SENTENCE" in prompt
    assert "ORIGINAL words" in prompt, "final pass re-reads the original request"
    assert "sensible default" in prompt, "clarify-vs-default decision rule"


def test_prompt_v7_puts_volatile_facts_in_the_tail(tmp_path):
    """Implicit-cache providers match the literal token prefix: a per-day
    Date at the HEAD broke the prefix for every new day and fresh session.
    Stable identity/rules come first; Date (and the git snapshot) last."""
    from oaset.agent.prompts import build_system_prompt

    prompt = build_system_prompt(tmp_path)
    head = prompt.index("# Operating rules")
    date = prompt.index("- Date:")
    assert date > head and prompt.index("# Now") < date
    assert "Date:" not in prompt[:head], "the stable head carries no per-day fact"


@pytest.mark.asyncio
async def test_anthropic_double_rolling_breakpoint_survives_idle_gaps():
    """tools + system + the last TWO user-turn boundaries = exactly the
    4-breakpoint budget; with one marker, a six-minute think between turns
    outlived the newest entry's 5-minute TTL and reprocessed the whole
    conversation uncached."""
    provider, captured = _anthropic_provider(lambda c: None, prompt_cache=True)
    async for _ in provider.stream([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
    ], None):
        pass
    msgs = captured["body"]["messages"]
    marked = [m for m in msgs if isinstance(m.get("content"), list)
              and any(b.get("cache_control") for b in m["content"])]
    assert len(marked) == 2, "the newest AND previous boundaries carry markers"
    assert msgs[-1]["content"][-1]["text"] == "second question"


@pytest.mark.asyncio
async def test_anthropic_one_hour_ttl_marker_and_beta_header():
    provider, captured = _anthropic_provider(lambda c: None, prompt_cache=True)
    provider.prompt_cache_ttl = "1h"
    tools = [{"type": "function",
              "function": {"name": "read_file", "parameters": {}}}]
    async for _ in provider.stream([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ], tools):
        pass
    body = captured["body"]
    assert body["system"][0]["cache_control"]["ttl"] == "1h"
    assert body["tools"][-1]["cache_control"]["ttl"] == "1h"
    assert body["messages"][-1]["content"][-1]["cache_control"]["ttl"] == "1h"
    # without the beta header the API silently honors the default 5m TTL
    assert "extended-cache-ttl-2025-04-11" in captured["beta"]


def test_prompt_cache_ttl_roundtrips(cfg):
    from oaset.config import load_config, save_config

    assert cfg.providers["mock"].prompt_cache_ttl == ""
    cfg.providers["mock"].prompt_cache_ttl = "1h"
    save_config(cfg)
    loaded = load_config()
    assert loaded.providers["mock"].prompt_cache_ttl == "1h"


# ------------------------------------------- bedrock closes the boundary

@pytest.mark.asyncio
async def test_bedrock_carries_the_same_cache_breakpoints():
    """Anthropic-on-Bedrock speaks the Messages wire format, so the same
    cache_control markers apply: system block, tools[-1], and the last two
    user-turn boundaries. This was the last documented cache boundary."""
    import json as _json

    from oaset.providers.bedrock_native import BedrockNativeProvider

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        })

    provider = BedrockNativeProvider(
        "bedrock/claude", access_key="AKIA", secret_key="sec",
        region="us-east-1", client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)),
        prompt_cache=True)
    tools = [{"type": "function",
              "function": {"name": "read_file", "parameters": {}}}]
    async for _ in provider.stream([
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ], tools):
        pass
    body = captured["body"]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    marked = [m for m in body["messages"] if isinstance(m.get("content"), list)
              and any(b.get("cache_control") for b in m["content"])]
    assert len(marked) == 2, "both rolling boundaries carry markers"


@pytest.mark.asyncio
async def test_context_warns_on_low_session_cache_reuse(workspace):
    """A low session reuse rate gets a diagnosis line, not just a number —
    each named cause has its own fix."""
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn
    from oaset.tui.app import OasetApp

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        monkey_session = object()
        app.session = monkey_session

        def low_summary(session):
            assert session is monkey_session
            return {"last": {"cache_read_tokens": 10, "prompt_tokens": 1000},
                    "prompt_tokens": 10000, "cache_read_tokens": 500,
                    "cache_write_tokens": 100, "total_tokens": 10100,
                    "entries": 4}  # 5% reuse across 4 turns

        app.store.usage_summary = low_summary  # type: ignore[method-assign]
        app.cmd_context("")
        await pilot.pause(0.05)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "5%" in rendered and ("compact" in rendered or "1h" in rendered)

        def high_summary(session):
            return {"last": {"cache_read_tokens": 900, "prompt_tokens": 1000},
                    "prompt_tokens": 10000, "cache_read_tokens": 9000,
                    "cache_write_tokens": 100, "total_tokens": 10100,
                    "entries": 4}

        app.store.usage_summary = high_summary  # type: ignore[method-assign]
        app.cmd_context("")
        await pilot.pause(0.05)
        fresh = [c for c in app.chat.children][-1]
        assert "90%" in str(fresh.render())
        assert "compact" not in str(fresh.render()), (
            "a healthy rate must not carry the low-reuse warning")


@pytest.mark.asyncio
async def test_plans_lists_forge_documents_newest_first(workspace):
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn

    plans = workspace / ".oaset" / "plans"
    plans.mkdir(parents=True)
    (plans / "web-panel.md").write_text(
        "# Web panel plan\nconfirmed", encoding="utf-8")
    (plans / "cli-todo.md").write_text(
        "# CLI todo plan\nconfirmed", encoding="utf-8")

    from oaset.tui.app import OasetApp

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.cmd_plans("")
        await pilot.pause(0.05)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "web-panel.md" in rendered and "cli-todo.md" in rendered
        assert "Web panel plan" in rendered
        assert "/view" in rendered


def test_subagent_prompts_share_the_parent_stable_prefix():
    """Cache property, pinned: every sub-agent's system prompt is the FULL
    parent prompt plus a role suffix APPENDED — the stable head is byte
    identical, so parent/sub-agent requests share the cached prefix within
    TTL (Anthropic system breakpoint; implicit prefixes everywhere else).
    Moving the role block ABOVE the stable content would silently break
    cross-agent cache hits."""
    from pathlib import Path

    from oaset.agent.prompts import build_system_prompt

    base = build_system_prompt(Path("."))
    for name, role in (("scout", "You explore."), ("builder", "You build.")):
        sub = build_system_prompt(Path(".")) + \
            f"\n\n# Your role as sub-agent '{name}'\n{role}\n"
        assert sub.startswith(base), (
            f"{name}'s prompt must keep the parent's stable prefix intact")
