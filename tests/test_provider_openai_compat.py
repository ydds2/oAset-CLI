"""OpenAICompatProvider against a scripted SSE stream via httpx MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest
from openai import AsyncOpenAI

from oaset.providers.base import (
    ContentDelta,
    ProviderError,
    ReasoningDelta,
    StreamDone,
    ToolCallEmitted,
)
from oaset.providers.openai_compat import OpenAICompatProvider


def sse_bytes(chunks: list[dict]) -> bytes:
    out = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)
    return out + b"data: [DONE]\n\n"


def chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> dict:
    payload = {
        "id": "x",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        payload["usage"] = usage
        payload["choices"] = []
    return payload


def make_provider(handler) -> OpenAICompatProvider:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AsyncOpenAI(api_key="k", base_url="http://test/v1", http_client=http, max_retries=0)
    return OpenAICompatProvider("prov/test-model", api_key="k", base_url="http://test/v1", client=client)


async def collect(provider, messages=None, tools=None):
    events = []
    async for event in provider.stream(messages or [{"role": "user", "content": "hi"}], tools):
        events.append(event)
    return events


async def test_content_and_reasoning_and_usage():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(
            200,
            content=sse_bytes(
                [
                    chunk({"reasoning_content": "think "}),
                    chunk({"content": "Hel"}),
                    chunk({"content": "lo"}),
                    chunk({}, usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}),
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider = make_provider(handler)
    events = await collect(provider)
    kinds = [type(e) for e in events]
    assert kinds.count(ContentDelta) == 2
    assert kinds.count(ReasoningDelta) == 1
    done = events[-1]
    assert isinstance(done, StreamDone) and done.finish_reason == "stop"
    assert done.usage["total_tokens"] == 7
    # wire format sanity: model name strips provider prefix
    assert captured["body"]["model"] == "test-model"
    assert captured["body"]["stream"] is True


async def test_tool_call_fragment_aggregation_by_index():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse_bytes(
                [
                    chunk({"tool_calls": [
                        {"index": 0, "id": "call_1", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"pat'}}]}),
                    chunk({"tool_calls": [
                        {"index": 0, "function": {"arguments": 'h": "a.txt"}'}}]}),
                    chunk({}, finish="tool_calls"),
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    events = await collect(make_provider(handler), tools=[{"type": "function", "function": {}}])
    calls = [e for e in events if isinstance(e, ToolCallEmitted)]
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "read_file"
    assert json.loads(calls[0].arguments) == {"path": "a.txt"}
    assert events[-1].finish_reason == "tool_calls"


async def test_auth_error_classification():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider = make_provider(handler)
    with pytest.raises(ProviderError) as excinfo:
        await collect(provider)
    assert excinfo.value.category == "auth"
    assert excinfo.value.retryable is False


async def test_rate_limit_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    provider = make_provider(handler)
    with pytest.raises(ProviderError) as excinfo:
        await collect(provider)
    assert excinfo.value.retryable is True


async def test_first_byte_watchdog_aborts_silent_endpoint():
    """Freeze-fix regression: an endpoint that accepts the request but never
    sends headers must fail FAST as retryable, not hold the turn for 300s."""
    import asyncio
    from types import SimpleNamespace

    async def hanging_create(**kwargs):
        await asyncio.sleep(60)
        raise AssertionError("watchdog must cancel create() first")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=hanging_create)))
    provider = OpenAICompatProvider("prov/test-model", api_key="k",
                                    base_url="http://test/v1", client=client,
                                    first_byte_timeout=0.3)
    with pytest.raises(ProviderError) as excinfo:
        async for _ in provider.stream([{"role": "user", "content": "hi"}], None):
            pass
    assert excinfo.value.retryable is True
    assert "no response headers" in str(excinfo.value.message)


async def test_consume_watchdog_allows_slow_reasoning_after_first_chunk():
    """Once the endpoint HAS streamed a first chunk, slow pauses between
    chunks are legitimate reasoning time — the watchdog must not fire."""
    import asyncio
    from types import SimpleNamespace

    provider = OpenAICompatProvider("prov/test-model", api_key="k",
                                    base_url="http://test/v1", client=object())
    provider._first_byte_timeout = 0.3

    def mk_chunk(content, finish=None):
        return SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(finish_reason=finish,
                                     delta=SimpleNamespace(content=content))])

    class SlowStream:
        def __init__(self):
            self.n = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.n += 1
            if self.n == 1:
                return mk_chunk("start")
            if self.n == 2:
                await asyncio.sleep(0.6)  # > watchdog: thinking pause
                return mk_chunk(" end", finish="stop")
            raise StopAsyncIteration

        async def close(self):
            pass

    events = [e async for e in provider._consume(SlowStream())]
    texts = [e.text for e in events if isinstance(e, ContentDelta)]
    assert "".join(texts) == "start end"
    assert any(isinstance(e, StreamDone) for e in events)
