"""Conversation message model — OpenAI wire format is the persistence format too."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from oaset.utils import estimate_tokens


@dataclass
class ToolCallReq:
    id: str
    name: str
    arguments: str  # JSON string
    # Gemini 3 thoughtSignature for this call (None elsewhere); must be
    # replayed on later requests or the API rejects the turn
    signature: str | None = None


#: User-role messages the agent writes itself: steering injections, the
#: verification nudge, background-task results and compaction summaries. They
#: travel on the wire as ordinary user messages, but they are not user *turns*.
#: The transcript renders them as notes, and /undo must not count them as
#: something the user asked for.
INJECTED_USER_PREFIXES = (
    "[mid-stream]",
    "[verify-gate]",
    "[background ",
    "[conversation summary",
)


def is_injected_user_text(text: str) -> bool:
    """True for the text of an agent-authored user-role message."""
    return str(text or "").lstrip().startswith(INJECTED_USER_PREFIXES)


def is_injected_user_message(message: "Message") -> bool:
    return message.role == "user" and is_injected_user_text(message.display_text())


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str | list | None = None
    tool_calls: list[ToolCallReq] | None = None
    tool_call_id: str | None = None
    reasoning: str | None = None  # display-only thinking stream
    # Anthropic/Gemini thinking-block signature: providers replay it with
    # the assistant turn during tool loops (a 400 without it)
    thinking_signature: str | None = None
    # the thinking was redacted (encrypted): replay is a verbatim
    # {"type": "redacted_thinking", "data": ...} block, not a signed one
    thinking_redacted: bool = False
    error: bool = False  # tool-result error flag (display/persistence only)
    partial: bool = False  # interrupted mid-stream marker (display only)

    def to_wire(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": self.role}
        if self.role == "tool":
            msg["tool_call_id"] = self.tool_call_id or ""
            msg["content"] = self.content if self.content is not None else ""
            return msg
        if self.role == "assistant":
            if self.content:
                msg["content"] = self.content
            # provider-consumed extras: anthropic replays the thinking
            # block+signature on tool loops; every OpenAI-wire serializer
            # strips them (unknown message fields are 400s there)
            if self.reasoning:
                msg["reasoning"] = self.reasoning
            if self.thinking_signature:
                msg["signature"] = self.thinking_signature
            if self.thinking_redacted:
                msg["redacted_thinking"] = True
            if self.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                        **({"signature": tc.signature} if tc.signature else {}),
                    }
                    for tc in self.tool_calls
                ]
        else:
            msg["content"] = self.content or ""
        return msg

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments,
                 **({"signature": tc.signature} if tc.signature else {})}
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.reasoning:
            out["reasoning"] = self.reasoning
        if self.thinking_signature:
            out["thinking_signature"] = self.thinking_signature
        if self.thinking_redacted:
            out["thinking_redacted"] = True
        if self.error:
            out["error"] = True
        if self.partial:
            out["partial"] = True
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        calls = data.get("tool_calls")
        return cls(
            role=data["role"],
            content=data.get("content"),
            tool_calls=[
                ToolCallReq(id=tc["id"], name=tc["name"],
                            arguments=tc.get("arguments", ""),
                            signature=tc.get("signature"))
                for tc in calls or []
            ]
            or None,
            tool_call_id=data.get("tool_call_id"),
            reasoning=data.get("reasoning"),
            thinking_signature=data.get("thinking_signature"),
            thinking_redacted=bool(data.get("thinking_redacted")),
            error=bool(data.get("error")),
            partial=bool(data.get("partial")),
        )

    def display_text(self) -> str:
        if isinstance(self.content, list):
            return "\n".join(
                str(part.get("text", "")) for part in self.content if isinstance(part, dict) and part.get("type") == "text"
            )
        return self.content or ""


class Conversation:
    """Ordered history. The system message is owned separately and rebuilt per session."""

    def __init__(self, system_prompt: str | None = None, messages: list[Message] | None = None):
        self.system_prompt = system_prompt
        self.messages: list[Message] = messages or []

    def append(self, message: Message) -> Message:
        self.messages.append(message)
        return message

    def wire_messages(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.system_prompt:
            out.append({"role": "system", "content": self.system_prompt})
        out.extend(m.to_wire() for m in self.messages)
        return out

    def token_estimate(self) -> int:
        total = estimate_tokens(self.system_prompt or "")
        for m in self.messages:
            content = m.content
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            total += estimate_tokens(str(part.get("text", "")))
                        elif part.get("type") == "image_url":
                            total += 750  # rough per-image cost
            else:
                total += estimate_tokens(content or "")
            for tc in m.tool_calls or []:
                total += estimate_tokens(tc.arguments) + estimate_tokens(tc.name)
        return total

    def to_dicts(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.messages]

    @classmethod
    def from_dicts(cls, dicts: list[dict[str, Any]], system_prompt: str | None = None) -> Conversation:
        return cls(system_prompt=system_prompt, messages=[Message.from_dict(d) for d in dicts])
