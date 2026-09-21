"""Scripted mock provider — deterministic offline agent loops for tests/demos."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from oaset.providers.base import (
    ContentDelta,
    ReasoningDelta,
    StreamDone,
    ToolCallEmitted,
)


@dataclass
class MockToolCall:
    name: str
    arguments: dict[str, Any] | str

    def args_json(self) -> str:
        if isinstance(self.arguments, str):
            return self.arguments
        return json.dumps(self.arguments)


@dataclass
class MockTurn:
    """One assistant turn: streamed chunks, optional tool calls, finish reason."""

    content_chunks: list[str] = field(default_factory=list)
    reasoning_chunks: list[str] = field(default_factory=list)
    tool_calls: list[MockToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, int] | None = None


class MockProvider:
    """Plays back a script of turns; when exhausted, echoes the last user text."""

    def __init__(self, script: list[MockTurn] | None = None, model_id: str = "mock/mock-echo"):
        self.model_id = model_id
        self.script = list(script or [])
        self.cursor = 0
        self.requests: list[list[dict[str, Any]]] = []

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> AsyncIterator[Any]:
        self.requests.append([dict(m) for m in messages])
        if self.cursor < len(self.script):
            turn = self.script[self.cursor]
            self.cursor += 1
        else:
            last_user = next(
                (m for m in reversed(messages) if m.get("role") == "user"), None
            )
            text = last_user.get("content", "") if last_user else ""
            turn = MockTurn(content_chunks=[f"[echo] {text}"], finish_reason="stop")
        for chunk in turn.reasoning_chunks:
            yield ReasoningDelta(text=chunk)
        for chunk in turn.content_chunks:
            yield ContentDelta(text=chunk)
        for i, call in enumerate(turn.tool_calls):
            yield ToolCallEmitted(
                index=i, id=f"call_{self.cursor}_{i}", name=call.name, arguments=call.args_json()
            )
        yield StreamDone(
            finish_reason=turn.finish_reason or ("tool_calls" if turn.tool_calls else "stop"),
            usage=turn.usage,
        )
