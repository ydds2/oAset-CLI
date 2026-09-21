"""Anthropic native provider - /v1/messages streaming SSE.

Same Provider protocol as OpenAICompatProvider (ContentDelta / ReasoningDelta /
ToolCallEmitted / StreamDone), so the agent loop is unchanged.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from oaset import __version__
from oaset.providers.base import (
    ContentDelta,
    ProviderError,
    ReasoningDelta,
    StreamDone,
    ThinkingRedacted,
    ThinkingSignature,
    ToolCallEmitted,
)

ANTHROPIC_VERSION = "2023-06-01"


def _classify(status: int, message: str) -> ProviderError:
    if status in (401, 403):
        return ProviderError("auth", message, retryable=False)
    if status == 429:
        return ProviderError("rate_limit", message, retryable=True)
    if status >= 500:
        return ProviderError("server", message, retryable=True)
    return ProviderError("bad_request", message, retryable=False)


def _anthropic_tool_content(content: Any) -> Any:
    """String, or a list of text + base64 image blocks (computer-use screenshots)."""
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                blocks.append({"type": "text", "text": str(part.get("text", ""))})
            elif part.get("type") == "image_url":
                url = str((part.get("image_url") or {}).get("url") or "")
                if url.startswith("data:image/") and ";base64," in url:
                    header, data = url.split(";base64,", 1)
                    media = header.split(":", 1)[-1] or "image/png"
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media, "data": data},
                    })
        return blocks or ""
    return str(content or "")


def _to_wire(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system = ""
    converted: list[dict[str, Any]] = []
    # the API only accepts a thinking block on the FINAL assistant message
    last_assistant_idx = max(
        (i for i, m in enumerate(messages) if m.get("role") == "assistant"),
        default=-1)
    for pos, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            system = str(content or "")
            continue
        if role == "tool":
            converted.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id", ""),
                    "content": _anthropic_tool_content(content),
                }],
            })
            continue
        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            # interleaved thinking: the API requires the (unmodified)
            # thinking block + signature of the last assistant turn to be
            # replayed alongside its tool_use blocks; omitting it made every
            # follow-up request in a thinking-enabled tool loop a 400
            if pos == last_assistant_idx and message.get("reasoning"):
                if message.get("redacted_thinking"):
                    blocks.append({"type": "redacted_thinking",
                                   "data": str(message["reasoning"])})
                elif message.get("signature"):
                    blocks.append({
                        "type": "thinking",
                        "thinking": str(message["reasoning"]),
                        "signature": str(message["signature"]),
                    })
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for call in message["tool_calls"]:
                raw = call.get("arguments") or "{}"
                try:
                    input_value = json.loads(raw)
                except json.JSONDecodeError:
                    input_value = {"_raw": raw}
                blocks.append({
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["function"]["name"],
                    "input": input_value,
                })
            converted.append({"role": "assistant", "content": blocks})
            continue
        converted.append({"role": role, "content": str(content or "")})
    return system, converted


def _apply_cache_breakpoints(system: str, converted: list[dict[str, Any]],
                              ttl: str = "") -> None:
    """Anthropic prompt caching (opt-in): mark the system block and the
    newest TWO user-turn boundaries with ephemeral cache_control.

    Markers only *write* cache entries — reads happen by prefix match —
    so one rolling marker on the final user message carries the
    conversation body across consecutive turns. The SECOND marker writes
    the previous boundary too: when the newest entry's 5-minute TTL
    lapses on an idle gap (the user thinking for six minutes), the
    previous entry still covers everything up to one turn back instead
    of the whole conversation re-processing uncached. tools[-1] + system
    + 2 rolling = exactly Anthropic's 4-breakpoint budget.
    """
    marked = 0
    for message in reversed(converted):
        if marked >= 2:
            break
        if message.get("role") != "user":
            continue
        content = message.get("content")
        ctrl = {"type": "ephemeral"}
        if ttl == "1h":
            ctrl["ttl"] = "1h"
        if isinstance(content, str) and content:
            message["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": dict(ctrl),
            }]
            marked += 1
        elif isinstance(content, list) and content and isinstance(content[-1], dict):
            content[-1]["cache_control"] = dict(ctrl)
            marked += 1


def _to_anthropic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    converted = []
    for tool in tools:
        if tool.get("type") not in (None, "function"):
            converted.append(tool)  # server tools (web_search_*) keep their shape
            continue
        fn = tool.get("function", tool)
        converted.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters")
            or {"type": "object", "properties": {}, "required": []},
        })
    return converted


class AnthropicNativeProvider:
    # build_provider sets this from ModelConfig.capabilities; models without
    # a "thinking" capability reject the thinking parameter with a 400
    thinking_supported = True
    def __init__(
        self,
        model_id: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        reasoning_key: str | None = None,
        timeout: float = 300.0,
        client: Any | None = None,
        policy: Any | None = None,
        prompt_cache: bool = False,
        prompt_cache_ttl: str = "",
    ):
        self.model_id = model_id
        self.prompt_cache = bool(prompt_cache)
        # /think level for this instance ("" = provider default); synced by
        # the agent loop from session_state before every model call
        self.thinking_level = ""
        # "" = default 5-minute cache TTL; "1h" opts into the extended-TTL
        # beta (writes cost 2x instead of 1.25x — worth it only for
        # slow-paced sessions whose gaps exceed five minutes)
        self.prompt_cache_ttl = str(prompt_cache_ttl or "").strip().lower()
        self._policy = policy
        self.model_name = model_id.split("/", 1)[-1]
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        if client is not None:
            self._client = client
        else:
            self._client = httpx.AsyncClient(
                timeout=timeout,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    # identify the client honestly: we are an independent
                    # client calling the documented API, not the vendor's tool
                    "user-agent": f"oaset-cli/{__version__}",
                },
            )

    def _cache_ctrl(self) -> dict[str, str]:
        ctrl = {"type": "ephemeral"}
        if self.prompt_cache_ttl == "1h":
            ctrl["ttl"] = "1h"
        return ctrl

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> AsyncIterator[Any]:
        system, converted = _to_wire(messages)
        if self.prompt_cache:
            _apply_cache_breakpoints(system, converted,
                                     ttl=self.prompt_cache_ttl)
        payload: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": 8192,
            "stream": True,
            "messages": converted,
        }
        if system:
            if self.prompt_cache:
                payload["system"] = [{
                    "type": "text",
                    "text": system,
                    "cache_control": self._cache_ctrl(),
                }]
            else:
                payload["system"] = system
        from oaset.capabilities import active_native_skills, native_payload

        server_tools, params, betas = native_payload(
            self.model_id, set(active_native_skills(self)))
        patched: list[dict[str, Any]] = []
        native_names: set[str] = set()
        for tool in server_tools:
            item = dict(tool)
            if item.get("name"):
                native_names.add(str(item["name"]))
            patched.append(item)
        # Provider-native schemas replace the ordinary function of the same
        # name so the model sees one of each, not two.
        converted = [
            t for t in (_to_anthropic_tools(tools) or [])
            if t.get("name") not in native_names
        ]
        anthropic_tools = [*patched, *converted] if (patched or converted) else None
        if anthropic_tools:
            if self.prompt_cache:
                # the tool table sits at the head of every request: one
                # breakpoint on its last element caches the whole table
                anthropic_tools[-1]["cache_control"] = self._cache_ctrl()
            payload["tools"] = anthropic_tools
        if params:
            payload.update(params)
        # user-facing thinking level (/think) overrides any capability
        # default; off deletes it entirely
        from oaset.thinking import apply_thinking

        if getattr(self, "thinking_supported", True):
            apply_thinking(payload, self.model_id,
                           getattr(self, "thinking_level", ""), api_format="anthropic")
        # The Messages API requires temperature=1 when extended thinking is
        # on — re-check after the level may have switched it on.
        if isinstance(payload.get("thinking"), dict):
            payload["temperature"] = 1
        else:
            # thinking is OFF for THIS request: replayed history thinking
            # blocks are rejected with a 400 — strip them (a session that
            # once thought, then /think off or switched to a model without
            # the capability, used to brick on its own history)
            for message in payload.get("messages", []):
                content = message.get("content")
                if isinstance(content, list):
                    message["content"] = [
                        b for b in content
                        if not (isinstance(b, dict) and b.get("type") in
                                ("thinking", "redacted_thinking"))
                    ]
        if self._policy is not None:
            self._policy.assert_allowed("model_call", endpoint=str(getattr(self, "base_url", "") or ""))
        if self.prompt_cache and self.prompt_cache_ttl == "1h":
            if "extended-cache-ttl-2025-04-11" not in betas:
                betas = (*betas, "extended-cache-ttl-2025-04-11")
        extra_headers = {"anthropic-beta": ",".join(betas)} if betas else {}
        request = self._client.build_request(
            "POST", f"{self.base_url}/v1/messages", json=payload,
            headers=extra_headers or None,
        )
        try:
            response = await self._client.send(request)
        except Exception as exc:
            raise ProviderError("network", str(exc), retryable=True) from exc
        if response.status_code >= 400:
            raise _classify(response.status_code, response.text[:300])

        try:
            tool_blocks: dict[int, dict[str, str]] = {}
            stop_reason = "stop"
            usage: dict[str, int] | None = None
            current_tool_index = -1
            signature_parts: list[str] = []
            redacted = False
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "redacted_thinking":
                        # encrypted thinking: replay verbatim later, never
                        # as a signed thinking block
                        redacted = True
                        if block.get("data"):
                            yield ReasoningDelta(text=str(block["data"]))
                    if block.get("type") == "tool_use":
                        current_tool_index = event.get("index", 0)
                        tool_blocks[current_tool_index] = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "arguments": "",
                        }
                elif kind == "content_block_delta":
                    delta = event.get("delta", {})
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        yield ContentDelta(text=delta.get("text", ""))
                    elif dtype in ("thinking_delta", "redacted_thinking_delta"):
                        yield ReasoningDelta(text=delta.get("thinking", delta.get("data", "")))
                    elif dtype == "signature_delta":
                        # the thinking block's proof: it must ride back on
                        # the next request during tool loops (a 400 without)
                        signature_parts.append(delta.get("signature", ""))
                    elif dtype == "input_json_delta":
                        if current_tool_index in tool_blocks:
                            tool_blocks[current_tool_index]["arguments"] += delta.get("partial_json", "")
                elif kind == "error":
                    # a mid-stream error event (documented, e.g. overloaded)
                    # used to fall through silently: the partial text was
                    # presented as a COMPLETE answer and no retry/fallback
                    # ever fired
                    err = event.get("error") or {}
                    status = {"overloaded_error": 529,
                              "rate_limit_error": 429,
                              "invalid_request_error": 400,
                              "authentication_error": 401,
                              "permission_error": 403}.get(str(err.get("type")), 500)
                    raise _classify(status, json.dumps(err, ensure_ascii=False)[:300])
                elif kind == "message_delta":
                    delta = event.get("delta", {})
                    if delta.get("stop_reason"):
                        stop_reason = delta["stop_reason"]
                    if event.get("usage"):
                        u = event["usage"]
                        # message_delta carries only output_tokens: MERGE over the
                        # message_start prompt count instead of zeroing it
                        prompt = int(u.get("input_tokens", 0) or 0)                             or int((usage or {}).get("prompt_tokens", 0) or 0)
                        completion = int(u.get("output_tokens", 0) or 0)
                        usage = {
                            "prompt_tokens": prompt,
                            "completion_tokens": completion,
                            "total_tokens": prompt + completion,
                            "cache_read_tokens": int(u.get("cache_read_input_tokens", 0) or 0)
                                                 or int((usage or {}).get("cache_read_tokens", 0) or 0),
                            "cache_write_tokens": int(u.get("cache_creation_input_tokens", 0) or 0)
                                                  or int((usage or {}).get("cache_write_tokens", 0) or 0),
                        }
                elif kind == "message_start":
                    u = (event.get("message", {}) or {}).get("usage")
                    if u:
                        usage = {
                            "prompt_tokens": int(u.get("input_tokens", 0) or 0),
                            "completion_tokens": int(u.get("output_tokens", 0) or 0),
                            "cache_read_tokens": int(u.get("cache_read_input_tokens", 0) or 0),
                            "cache_write_tokens": int(u.get("cache_creation_input_tokens", 0) or 0),
                        }
        finally:
            await response.aclose()
        if "".join(signature_parts):
            yield ThinkingSignature(text="".join(signature_parts))
        if redacted:
            yield ThinkingRedacted()
        for index in sorted(tool_blocks):
            block = tool_blocks[index]
            yield ToolCallEmitted(
                index=index, id=block["id"], name=block["name"], arguments=block["arguments"] or "{}"
            )
        yield StreamDone(
            finish_reason="tool_calls" if stop_reason == "tool_use" else stop_reason,
            usage=usage,
        )
