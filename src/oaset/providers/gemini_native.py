"""Gemini native provider - streamGenerateContent?alt=sse.

Converts oAset wire messages/tools to Gemini's contents/functionDeclarations
and streams SSE: candidates[].content.parts[].text and functionCall,
finishReason STOP/MAX_TOKENS, usageMetadata.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from oaset.providers.base import (
    ContentDelta,
    ProviderError,
    ReasoningDelta,
    StreamDone,
    ToolCallEmitted,
)


def _classify(status: int, message: str) -> ProviderError:
    if status in (401, 403):
        return ProviderError("auth", message, retryable=False)
    if status == 429:
        return ProviderError("rate_limit", message, retryable=True)
    if status >= 500:
        return ProviderError("server", message, retryable=True)
    return ProviderError("bad_request", message, retryable=False)


def _gemini_text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(
            str(p.get("text", "")) for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content or "")


def _gemini_tool_parts(content: Any) -> tuple[str, list[dict[str, Any]]]:
    """Split a tool result into text + optional inline screenshot parts."""
    if isinstance(content, list):
        texts: list[str] = []
        images: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                texts.append(str(part.get("text", "")))
            elif part.get("type") == "image_url":
                url = str((part.get("image_url") or {}).get("url") or "")
                if url.startswith("data:image/") and ";base64," in url:
                    header, data = url.split(";base64,", 1)
                    mime = header.split(":", 1)[-1] or "image/png"
                    images.append({"inlineData": {"mimeType": mime, "data": data}})
        return "\n".join(texts), images
    return str(content or ""), []


def _to_gemini(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system = ""
    contents: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "assistant":
            for call in (m.get("tool_calls") or []):
                if isinstance(call, dict):
                    fn = call.get("function") or {}
                    call_names[str(call.get("id") or "")] = str(fn.get("name") or "tool")
        if role == "system":
            system = _gemini_text(content)
            continue
        if role == "tool":
            text, images = _gemini_tool_parts(content)
            # the wire message carries no name; recover it from the assistant
            # turn's tool_calls (a placeholder name mismatches the declared
            # function and is a hard 400 on real Gemini)
            tool_name = m.get("name") or call_names.get(str(m.get("tool_call_id")), "tool")
            parts: list[dict[str, Any]] = [{"functionResponse": {
                "name": tool_name,
                "response": {"result": text},
            }}]
            parts.extend(images)
            # PARALLEL tool calls must merge into ONE user content: Gemini
            # requires every functionResponse of a model turn in the single
            # immediately-following user part list — splitting them is a hard
            # 400 ("number of function responses != number of function calls")
            if contents and contents[-1].get("role") == "user"                     and contents[-1]["parts"] and                     "functionResponse" in contents[-1]["parts"][0]:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": "user", "parts": parts})
            continue
        if role == "assistant" and m.get("tool_calls"):
            model_parts: list[dict[str, Any]] = []
            if content:
                model_parts.append({"text": content})
            for call in m["tool_calls"]:
                try:
                    args = json.loads(call.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                part: dict[str, Any] = {
                    "functionCall": {"name": call["function"]["name"],
                                     "args": args}}
                # Gemini 3 attaches a thoughtSignature to function calls
                # made under thinking; the API requires it replayed on
                # later turns (a 4xx without it)
                if call.get("signature"):
                    part["thoughtSignature"] = call["signature"]
                model_parts.append(part)
            contents.append({"role": "model", "parts": model_parts})
            continue
        contents.append({"role": "model" if role == "assistant" else "user",
                         "parts": [{"text": _gemini_text(content)}]})
    return system, contents


_GEMINI_NATIVE_KEYS = ("google_search", "url_context", "code_execution", "functionDeclarations")


def _to_gemini_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    decls = []
    passthrough = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if any(key in tool for key in _GEMINI_NATIVE_KEYS):
            passthrough.append(tool)
            continue
        fn = tool.get("function", tool)
        name = fn.get("name") if isinstance(fn, dict) else None
        if not name:
            passthrough.append(tool)
            continue
        decls.append({
            "name": name,
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    out: list[dict[str, Any]] = []
    if decls:
        out.append({"functionDeclarations": decls})
    out.extend(passthrough)
    return out or None


class GeminiNativeProvider:
    # build_provider sets this from ModelConfig.capabilities
    thinking_supported = True
    def __init__(
        self,
        model_id: str,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com",
        reasoning_key: str | None = None,
        timeout: float = 300.0,
        client: Any | None = None,
        policy: Any | None = None,
    ):
        self.model_id = model_id
        self.model_name = model_id.split("/", 1)[-1]
        self.base_url = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
        self.api_key = api_key
        self._policy = policy
        # /think level ("" = provider default); synced by the agent loop
        self.thinking_level = ""
        if client is not None:
            self._client = client
        else:
            self._client = httpx.AsyncClient(timeout=timeout, params={"key": api_key})

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> AsyncIterator[Any]:
        if self._policy is not None:
            self._policy.assert_allowed("model_call", endpoint=str(getattr(self, "base_url", "") or ""))
        system, contents = _to_gemini(messages)
        body: dict[str, Any] = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        gemini_tools = _to_gemini_tools(tools)
        from oaset.capabilities import active_native_skills, native_payload

        server_tools, params, _headers = native_payload(
            self.model_id, set(active_native_skills(self)))
        if server_tools:
            gemini_tools = [*server_tools, *(gemini_tools or [])]
        if gemini_tools:
            body["tools"] = gemini_tools
        if params:
            for key, value in params.items():
                existing = body.get(key)
                if isinstance(value, dict) and isinstance(existing, dict):
                    existing.update(value)
                else:
                    body[key] = value
        # /think level → thinkingConfig.thinkingBudget (deep-merged: a
        # native capability may already have set includeThoughts)
        from oaset.thinking import apply_thinking

        if getattr(self, "thinking_supported", True):
            apply_thinking(body, self.model_id, getattr(self, "thinking_level", ""),
                           api_format="gemini")
        url = (
            f"{self.base_url}/v1beta/models/{self.model_name}"
            f":streamGenerateContent?alt=sse"
        )
        request = self._client.build_request("POST", url, json=body)
        try:
            response = await self._client.send(request)
        except Exception as exc:
            raise ProviderError("network", str(exc), retryable=True) from exc
        if response.status_code >= 400:
            raise _classify(response.status_code, response.text[:300])

        tool_index = 0
        usage: dict[str, int] | None = None
        finish = "stop"
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if event.get("error"):
                # mid-stream error object: raising (instead of ending the
                # stream "normally") is what engages retry/fallback
                err = event["error"]
                raise ProviderError(
                    "server", json.dumps(err, ensure_ascii=False)[:300],
                    retryable=True)
            for candidate in event.get("candidates", []):
                for part in (candidate.get("content", {}) or {}).get("parts", []) or []:
                    if "text" in part:
                        if part.get("thought"):
                            yield ReasoningDelta(text=part["text"])
                        else:
                            yield ContentDelta(text=part["text"])
                    elif "functionCall" in part:
                        call = part["functionCall"]
                        yield ToolCallEmitted(
                            index=tool_index,
                            id=f"gem_{tool_index}",
                            name=call.get("name", ""),
                            arguments=json.dumps(call.get("args", {})),
                            signature=part.get("thoughtSignature"),
                        )
                        tool_index += 1
                if candidate.get("finishReason"):
                    reason = candidate["finishReason"]
                    finish = "tool_calls" if reason == "STOP" and tool_index else (
                        "stop" if reason == "STOP" else reason
                    )
            if event.get("usageMetadata"):
                u = event["usageMetadata"]
                usage = {
                    "prompt_tokens": int(u.get("promptTokenCount", 0) or 0),
                    "completion_tokens": int(u.get("candidatesTokenCount", 0) or 0),
                    "total_tokens": int(u.get("totalTokenCount", 0) or 0),
                }
                # Gemini implicit caching reports the reused prefix here;
                # without it /context claims "no cache data" on a provider
                # that was quietly hitting cache all along.
                cached = int(u.get("cachedContentTokenCount", 0) or 0)
                if cached:
                    usage["cache_read_tokens"] = cached
        yield StreamDone(finish_reason=finish, usage=usage)
