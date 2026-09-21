"""Sampling bridge: serve MCP sampling/createMessage via the live provider.

Flow: server requests a completion → permission gate approves (UIGate asks
the user, AutoGate allows, ReadOnlyGate denies) → ONE controlled completion
through the current provider (no tools, no agent loop — a single stream) →
reply in the MCP sampling result shape. A denial returns None, which the
client turns into JSON-RPC -32002.
"""

from __future__ import annotations

from typing import Any


def _message_text(content: Any) -> str:
    """MCP content may be a single block or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text", ""))
    if isinstance(content, list):
        return "\n".join(_message_text(block) for block in content)
    return ""


def _to_provider_messages(mcp_messages: list[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for msg in mcp_messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "user"))
        if role not in ("user", "assistant"):
            continue
        text = _message_text(msg.get("content"))
        if text:
            out.append({"role": role, "content": text})
    return out or [{"role": "user", "content": "(empty sampling request)"}]


def make_sampling_bridge(provider: Any, gate: Any, log: Any = None):
    """Build the async sampling handler for McpConnection.sampling_handler."""

    async def handler(params: dict[str, Any]) -> dict[str, Any] | None:
        from oaset.providers.base import ContentDelta, StreamDone

        mcp_messages = list(params.get("messages") or [])
        max_tokens = int(params.get("maxTokens") or 512)
        prompt_preview = _message_text(
            next((m.get("content") for m in mcp_messages
                  if isinstance(m, dict) and m.get("role") == "user"), "")
        )[:200]
        summary = (f"MCP server requests a model completion "
                   f"({len(mcp_messages)} messages, ≤{max_tokens} tokens)")
        decision = await gate.request("mcp_sampling", "exec", summary, prompt_preview or None)
        if not decision or str(decision).lower() in ("deny", "denied"):
            if log:
                log("sampling request denied by gate")
            return None

        chunks: list[str] = []
        stop_reason = "end_turn"
        async for event in provider.stream(_to_provider_messages(mcp_messages), []):
            if isinstance(event, ContentDelta):
                chunks.append(event.text)
            elif isinstance(event, StreamDone):
                stop_reason = "max_tokens" if event.finish_reason == "length" else "end_turn"
        return {
            "role": "assistant",
            "content": {"type": "text", "text": "".join(chunks)},
            "model": getattr(provider, "model_id", "unknown"),
            "stopReason": stop_reason,
        }

    return handler
