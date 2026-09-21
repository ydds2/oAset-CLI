"""OpenAI-compatible streaming provider (chat.completions + tools + thinking).

Handles the streaming tool-call protocol: `delta.tool_calls` fragments arrive
keyed by `index`; id/name/arguments are accumulated until finish_reason is
`tool_calls`, then re-emitted as complete ToolCallEmitted events. Thinking
models expose their reasoning through a configurable delta field
(reasoning_key, default `reasoning_content`) — the same knob Kimi Code's
config.toml exposes per model.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from oaset.providers.base import (
    ContentDelta,
    ProviderError,
    ReasoningDelta,
    StreamDone,
    ToolCallEmitted,
)


class OpenAICompatProvider:
    # build_provider sets this from ModelConfig.capabilities; sending
    # reasoning_effort to a non-reasoning model is a 400
    thinking_supported = True
    def __init__(
        self,
        model_id: str,
        api_key: str,
        base_url: str,
        reasoning_key: str | None = None,
        timeout: float = 300.0,
        client: Any | None = None,
        policy: Any | None = None,
        first_byte_timeout: float = 45.0,
    ):
        self.model_id = model_id
        self._policy = policy  # oaset.network.NetworkPolicy (None = unrestricted)
        self._reasoning_key = reasoning_key or "reasoning_content"
        # /think level ("" = provider default); synced by the agent loop
        self.thinking_level = ""
        # "Connected but silent" watchdog: a proxy that accepts TCP and never
        # answers otherwise looks like a 300s freeze per retry.
        self._first_byte_timeout = max(1.0, float(first_byte_timeout))
        if client is not None:  # injection point for tests (httpx MockTransport)
            self._client = client
        else:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=api_key or "EMPTY",
                base_url=base_url or None,
                timeout=timeout,
                max_retries=0,  # retries are owned by the agent loop
            )

    async def aclose(self) -> None:
        """Release the underlying HTTP client (call on app/CLI shutdown)."""
        client = getattr(self, "_client", None)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    @staticmethod
    def _sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip provider-consumed extras the OpenAI wire does not define.

        Wire dicts carry anthropic/gemini thinking-replay fields
        (message-level reasoning/signature, per-call signature); unknown
        message fields are 400s on this wire."""
        out: list[dict[str, Any]] = []
        for m in messages or []:
            m = {k: v for k, v in m.items() if k not in ("reasoning", "signature")}
            calls = m.get("tool_calls")
            if calls:
                m["tool_calls"] = [
                    {k: v for k, v in c.items() if k != "signature"} for c in calls
                ]
            out.append(m)
        return out

    def _serialize_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """$-prefixed builtin tools ship only to the provider that runs them
        (client_tool mechanism); every other provider strips them so a
        fallback never sends an unknown tool type."""
        from oaset.capabilities import capabilities_for

        caps = capabilities_for(self.model_id, None)
        has_client_tool = any(c.mechanism == "client_tool" for c in caps)
        if has_client_tool:
            return list(tools)
        from oaset.capabilities import split_native_tools

        return split_native_tools(tools)[0]

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> AsyncIterator[Any]:
        kwargs: dict[str, Any] = {
            "model": self.model_id.split("/", 1)[-1],
            "messages": self._sanitize_messages(messages),
            "stream": True,
        }
        kwargs["stream_options"] = {"include_usage": True}
        if tools:
            kwargs["tools"] = self._serialize_tools(tools)
        # native skill flags are set by runtime per provider instance
        from oaset.capabilities import active_native_skills, native_payload

        _server_tools, params, _headers = native_payload(
            self.model_id, set(active_native_skills(self)))
        if params:
            kwargs.update(params)
        # /think level → reasoning_effort (openai/azure) or thinking toggle
        # (glm/qwen); merged as plain top-level kwargs, matching each
        # provider's published OpenAI-compatible parameter
        from oaset.thinking import apply_thinking

        if getattr(self, "thinking_supported", True):
            apply_thinking(kwargs, self.model_id, getattr(self, "thinking_level", ""),
                           api_format="openai")
        if self._policy is not None:
            self._policy.assert_allowed("model_call", endpoint=str(self._client.base_url or ""))
        try:
            stream = await asyncio.wait_for(
                self._client.chat.completions.create(**kwargs),
                timeout=self._first_byte_timeout,
            )
        except TimeoutError:
            raise ProviderError(
                "network",
                f"no response headers within {self._first_byte_timeout:.0f}s",
                retryable=True,
            ) from None
        except Exception as exc:  # classify before streaming starts
            raise _classify(exc) from exc
        try:
            async for event in self._consume(stream):
                yield event
        except openai_error_types() as exc:
            raise _classify(exc) from exc

    async def _consume(self, stream: Any) -> AsyncIterator[Any]:
        tool_calls: dict[int, dict[str, str]] = {}
        finish_reason = "stop"
        usage: dict[str, int] | None = None
        first_chunk = True
        try:
            while True:
                try:
                    if first_chunk:
                        # only the WAIT FOR FIRST DATA is watchdogged; once the
                        # endpoint streams, slow reasoning pauses are legitimate
                        chunk = await asyncio.wait_for(
                            stream.__anext__(), timeout=self._first_byte_timeout)
                        first_chunk = False
                    else:
                        chunk = await stream.__anext__()
                except TimeoutError:
                    raise ProviderError(
                        "network",
                        f"no response data within {self._first_byte_timeout:.0f}s",
                        retryable=True,
                    ) from None
                except StopAsyncIteration:
                    break
                if getattr(chunk, "usage", None) is not None:
                    usage = _usage_dict(chunk.usage)
                if not getattr(chunk, "choices", None):
                    continue
                choice = chunk.choices[0]
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                reasoning = _first_not_none(
                    getattr(delta, self._reasoning_key, None),
                    getattr(delta, "reasoning", None),
                )
                if reasoning:
                    yield ReasoningDelta(text=reasoning)
                if delta.content:
                    yield ContentDelta(text=delta.content)
                for frag in getattr(delta, "tool_calls", None) or []:
                    slot = tool_calls.setdefault(
                        frag.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if frag.id:
                        slot["id"] = frag.id
                    func = getattr(frag, "function", None)
                    if func is not None:
                        if func.name:
                            slot["name"] = (
                                func.name if not slot["name"] else slot["name"]
                            )
                        if func.arguments:
                            slot["arguments"] += func.arguments
        finally:
            # deterministic SSE cleanup: avoids async-generator noise at teardown
            aclose = getattr(stream, "close", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
        for index in sorted(tool_calls):
            tc = tool_calls[index]
            yield ToolCallEmitted(
                index=index,
                id=tc["id"] or f"call_{index}",
                name=tc["name"],
                arguments=tc["arguments"] or "{}",
            )
        yield StreamDone(finish_reason=finish_reason or "stop", usage=usage)


# ---------------------------------------------------------------- helpers


def openai_error_types() -> tuple[type[Exception], ...]:
    import openai

    return (
        openai.AuthenticationError,
        openai.PermissionDeniedError,
        openai.RateLimitError,
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.BadRequestError,
        openai.NotFoundError,
        openai.InternalServerError,
        openai.UnprocessableEntityError,
    )


def _classify(exc: Exception) -> ProviderError:
    import openai

    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return ProviderError("auth", str(exc), retryable=False)
    if isinstance(exc, openai.RateLimitError):
        return ProviderError("rate_limit", str(exc), retryable=True)
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
        return ProviderError("network", str(exc), retryable=True)
    if isinstance(exc, (openai.BadRequestError, openai.UnprocessableEntityError)):
        return ProviderError("bad_request", str(exc), retryable=False)
    if isinstance(exc, (openai.NotFoundError, openai.InternalServerError)):
        return ProviderError("server", str(exc), retryable=True)
    return ProviderError("other", str(exc), retryable=False)


def _first_not_none(*values: Any) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


def _usage_dict(usage: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if value is not None:
            out[key] = int(value)
    # OpenAI-compatible implicit caching reports hits here (no markers exist
    # on this wire format — caching is server-side and automatic)
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None)
    if cached is not None:
        out["cache_read_tokens"] = int(cached)
    else:
        # DeepSeek reports hits as top-level prompt_cache_hit_tokens on the
        # same wire format; openai-python exposes unknown fields via getattr
        hit = getattr(usage, "prompt_cache_hit_tokens", None)
        if hit is not None:
            out["cache_read_tokens"] = int(hit)
    return out
