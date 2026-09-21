"""Bedrock native provider - AWS SigV4-signed invoke (non-stream), locally testable.

Implements the Bedrock runtime `InvokeModel` call with locally computed SigV4
signing (hmac/hashlib only - no AWS SDK), converts Anthropic-style Bedrock
responses to oAset stream events, and yields the full reply as one delta
(Bedrock's binary event-stream for true streaming is deferred; the agent loop
is agnostic). Credentials come from config/env at build time.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from oaset.providers.base import (
    ContentDelta,
    ProviderError,
    ReasoningDelta,
    StreamDone,
    ThinkingRedacted,
    ThinkingSignature,
    ToolCallEmitted,
)


def _sigv4_headers(
    method: str,
    host: str,
    path: str,
    body: bytes,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Minimal AWS SigV4 (service: bedrock) computed locally."""
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    service = "bedrock"
    canonical_headers = (
        f"content-type:application/json\nhost:{host}\nx-amz-content-sha256:"
        f"{hashlib.sha256(body).hexdigest()}\nx-amz-date:{amz_date}\n"
    )
    signed = "content-type;host;x-amz-content-sha256;x-amz-date"
    canonical = "\n".join(
        [method, path, "", canonical_headers, signed, hashlib.sha256(body).hexdigest()]
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    k_date = hmac.new(f"AWS4{secret_key}".encode(), date_stamp.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
    k_signing = hmac.new(k_region and k_service, b"aws4_request", hashlib.sha256).digest()
    signature = hmac.new(
        k_signing, f"{scope}\n{canonical}".encode(), hashlib.sha256
    ).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    headers = {
        "Authorization": auth,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": hashlib.sha256(body).hexdigest(),
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    return headers


def _to_bedrock_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system = ""
    converted: list[dict[str, Any]] = []
    # thinking blocks are only accepted on the FINAL assistant message
    last_assistant_idx = max(
        (i for i, m in enumerate(messages) if m.get("role") == "assistant"),
        default=-1)
    for i, m in enumerate(messages):
        role = m.get("role")
        content: Any = m.get("content")
        if role == "system":
            system = str(content or "")
            continue
        if isinstance(content, list):
            content = " ".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        if role == "tool":
            converted.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                             "content": str(m.get("content", ""))}],
            })
        elif role == "assistant" and m.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            # interleaved thinking: Bedrock requires the last assistant
            # turn's thinking block replayed with its tool_use — signed, or
            # verbatim redacted; omitting either shape is a 400
            if i == last_assistant_idx and m.get("reasoning"):
                if m.get("redacted_thinking"):
                    blocks.append({"type": "redacted_thinking",
                                   "data": m["reasoning"]})
                elif m.get("signature"):
                    blocks.append({"type": "thinking",
                                   "thinking": m["reasoning"],
                                   "signature": m["signature"]})
            if content:
                blocks.append({"type": "text", "text": content})
            for call in m["tool_calls"]:
                try:
                    input_value = json.loads(call.get("arguments") or "{}")
                except json.JSONDecodeError:
                    input_value = {}
                blocks.append({"type": "tool_use", "id": call["id"],
                               "name": call["function"]["name"], "input": input_value})
            converted.append({"role": "assistant", "content": blocks})
        else:
            converted.append({"role": role, "content": content or ""})
    return system, converted


class BedrockNativeProvider:
    """Bedrock InvokeModel adapter (non-streaming; yields the reply as one delta)."""

    # build_provider sets this from ModelConfig.capabilities
    thinking_supported = True

    def __init__(
        self,
        model_id: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        session_token: str | None = None,
        base_url: str = "",
        reasoning_key: str | None = None,
        timeout: float = 300.0,
        client: Any | None = None,
        policy: Any | None = None,
        prompt_cache: bool = False,
    ):
        self.model_id = model_id
        self.bedrock_model_id = model_id.split("/", 1)[-1]
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.base_url = base_url or f"https://bedrock-runtime.{region}.amazonaws.com"
        self._policy = policy
        # Anthropic-on-Bedrock speaks the same Messages wire format, so the
        # same cache_control breakpoints apply (system + tools[-1] + two
        # rolling user-turn boundaries)
        self.prompt_cache = bool(prompt_cache)
        # /think level ("" = provider default); synced by the agent loop
        self.thinking_level = ""
        if client is not None:
            self._client = client
        else:
            self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> AsyncIterator[Any]:
        if self._policy is not None:
            self._policy.assert_allowed("model_call", endpoint=str(getattr(self, "base_url", "") or ""))
        system, converted = _to_bedrock_messages(messages)
        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 8192,
            "messages": converted,
        }
        if system:
            body["system"] = system
        if tools:
            converted = []
            for tool in tools:
                fn = tool.get("function", tool)
                converted.append({
                    "type": "tool",
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object"},
                })
            body["tools"] = converted
        if self.prompt_cache:
            from oaset.providers.anthropic_native import _apply_cache_breakpoints

            _apply_cache_breakpoints(system, body["messages"])
            if system:
                body["system"] = [{"type": "text", "text": system,
                                   "cache_control": {"type": "ephemeral"}}]
            if body.get("tools"):
                body["tools"][-1]["cache_control"] = {"type": "ephemeral"}
        # /think level → anthropic-style thinking budget (same semantics as
        # the native Messages API, including the temperature=1 requirement)
        from oaset.thinking import apply_thinking

        if getattr(self, "thinking_supported", True):
            apply_thinking(body, self.model_id,
                           getattr(self, "thinking_level", ""), api_format="bedrock")
        if isinstance(body.get("thinking"), dict):
            body["temperature"] = 1
        else:
            # thinking OFF for THIS request: strip replayed history thinking
            # blocks (the API rejects them with a 400)
            for message in body.get("messages", []):
                content = message.get("content")
                if isinstance(content, list):
                    message["content"] = [
                        b for b in content
                        if not (isinstance(b, dict) and b.get("type") in
                                ("thinking", "redacted_thinking"))
                    ]

        host = self.base_url.split("//", 1)[-1]
        path = f"/model/{quote(self.bedrock_model_id, safe='')}/invoke"
        payload = json.dumps(body).encode()
        headers = _sigv4_headers(
            "POST", host, path, payload, self.region,
            self.access_key, self.secret_key, self.session_token,
        )
        headers["content-type"] = "application/json"
        headers["accept"] = "application/json"
        try:
            resp = await self._client.post(f"{self.base_url}{path}", content=payload, headers=headers)
        except Exception as exc:
            raise ProviderError("network", str(exc), retryable=True) from exc
        if resp.status_code >= 400:
            raise ProviderError(
                "auth" if resp.status_code in (401, 403) else
                "rate_limit" if resp.status_code == 429 else
                "server" if resp.status_code >= 500 else "bad_request",
                resp.text[:300], retryable=resp.status_code in (429, 500, 502, 503, 504),
            )
        data = resp.json()
        text_parts, tool_parts = [], []
        thinking_text, thinking_sig, redacted = "", "", False
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_parts.append(block)
            elif block.get("type") == "thinking":
                # InvokeModel returns the block complete: capture text AND
                # signature or the tool-loop replay (and the API) breaks.
                # ACCUMULATE: a response with several thinking blocks must
                # not silently keep only the last one.
                thinking_text += str(block.get("thinking", ""))
                thinking_sig += str(block.get("signature", ""))
            elif block.get("type") == "redacted_thinking":
                # sticky: mixed responses keep the flag for the replay shape
                redacted = True
                thinking_text += str(block.get("data", ""))
        if thinking_text:
            yield ReasoningDelta(text=thinking_text)
        if thinking_sig:
            yield ThinkingSignature(text=thinking_sig)
        if redacted:
            yield ThinkingRedacted()
        text = "".join(text_parts)
        if text:
            yield ContentDelta(text=text)
        for i, block in enumerate(tool_parts):
            yield ToolCallEmitted(
                index=i,
                id=block.get("id", f"toolu_{i}"),
                name=block.get("name", ""),
                arguments=json.dumps(block.get("input", {})),
            )
        stop = data.get("stop_reason", "end_turn")
        usage_raw = data.get("usage", {})
        yield StreamDone(
            finish_reason="tool_calls" if stop == "tool_use" else stop,
            usage={
                "prompt_tokens": int(usage_raw.get("input_tokens", 0) or 0),
                "completion_tokens": int(usage_raw.get("output_tokens", 0) or 0),
                "total_tokens": int(usage_raw.get("input_tokens", 0) or 0)
                + int(usage_raw.get("output_tokens", 0) or 0),
            },
        )
