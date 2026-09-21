"""Provider abstraction and stream event types.

The wire format is the OpenAI chat-completions message schema — the lingua
franca used by the mainstream terminal agents (every provider is routed
through OpenAI-compatible shapes; some vendors declare `type = "openai"`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class ContentDelta:
    """Incremental assistant text."""

    text: str


@dataclass
class ReasoningDelta:
    """Incremental thinking text (delta[reasoning_key], e.g. reasoning_content)."""

    text: str


@dataclass
class ThinkingSignature:
    """The cryptographic signature of one completed thinking block.

    Anthropic (and Gemini 3 via thoughtSignature) require the signature to
    be replayed with the assistant turn during tool loops; dropping it
    turned every follow-up request into a 400."""

    text: str


@dataclass
class ThinkingRedacted:
    """The turn's thinking was encrypted (redacted_thinking): the block
    must be replayed verbatim ({type: redacted_thinking, data}) instead of
    a signed thinking block."""

    flag: bool = True


@dataclass
class ToolCallEmitted:
    """A fully aggregated tool call (fragments accumulated by index)."""

    index: int
    id: str
    name: str
    arguments: str  # JSON string
    # Gemini 3 thoughtSignature attached to this functionCall: must ride
    # back on the next request or the API rejects the turn (4xx)
    signature: str | None = None


@dataclass
class StreamDone:
    finish_reason: str
    usage: dict[str, int] | None = None


StreamEvent = (ContentDelta | ReasoningDelta | ToolCallEmitted
               | ThinkingSignature | ThinkingRedacted | StreamDone)


class ProviderError(Exception):
    """Classified provider failure; `retryable` drives the agent-loop retry policy."""

    CATEGORIES = ("auth", "rate_limit", "network", "bad_request", "server", "other")

    def __init__(self, category: str, message: str, retryable: bool = False):
        super().__init__(message)
        if category not in self.CATEGORIES:
            category = "other"
        self.category = category
        self.message = message
        self.retryable = retryable

    def hint(self) -> str:
        if self.category == "auth":
            return ("Store a key with /login <provider> (saved under ~/.oaset/credentials/), "
                    "or set the env:VAR entry / edit ~/.oaset/config.toml.")
        if self.category == "rate_limit":
            return "Rate limited by the endpoint; retry shortly or switch models with /model."
        if self.category == "network":
            return "Could not reach the endpoint; check base_url and connectivity."
        if self.category == "bad_request":
            return "The request was rejected (possibly an unsupported parameter for this model)."
        return ""


class Provider(Protocol):
    """Minimal provider interface implemented by OpenAICompatProvider / MockProvider."""

    model_id: str

    def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> AsyncIterator[StreamEvent]: ...


def usage_total_tokens(usage: dict[str, int] | None) -> int | None:
    if not usage:
        return None
    total = usage.get("total_tokens")
    if total is not None:
        return int(total)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is not None and completion is not None:
        return int(prompt) + int(completion)
    return None
