"""Context management: token estimation, safe trimming, /compact summarization."""

from __future__ import annotations

from typing import Any

from oaset.agent.messages import Conversation, Message
from oaset.utils import estimate_tokens

SUMMARY_KEEP_RECENT = 4  # messages preserved verbatim after compaction
COMPACT_PROMPT = (
    "Summarize the conversation above for a coding assistant that will continue "
    "the work with no other memory. Keep: the user's goal, key decisions, files "
    "touched (with paths), commands run and their outcomes, and the exact next "
    "steps. Be under 400 words. Output the summary only."
)


def safe_boundary(msgs: list["Message"], index: int) -> int:
    """Largest position ``<= index`` that is safe to cut the history at.

    A turn is a unit on the wire: an ``assistant`` message carrying
    ``tool_calls`` and the ``tool`` results answering it must stay together.
    Cutting between them leaves a ``tool`` message the provider rejects, so a
    boundary that lands mid-turn walks back to the user message that opened
    the turn. ``len(msgs)`` (nothing kept) is returned unchanged — an empty
    window cannot be orphaned.
    """
    i = max(0, min(int(index), len(msgs)))
    while 0 < i < len(msgs) and msgs[i].role != "user":
        i -= 1
    return i


def turn_boundary(msgs: list["Message"], keep_recent: int) -> int:
    """Split index keeping the last ``keep_recent`` messages as whole turns.

    The count is a floor, not a rule — see :func:`safe_boundary`. Returns 0
    when no safe split exists, which callers read as "nothing to compact".
    """
    return safe_boundary(msgs, len(msgs) - max(0, int(keep_recent)))


def wire_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        total += estimate_tokens(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            total += estimate_tokens(tc["function"]["arguments"]) + 8
    return total


def conversation_tokens(conv: Conversation) -> int:
    return wire_tokens(conv.wire_messages())


def trim_for_context(
    conv: Conversation, max_context: int, reserve: int = 4000
) -> list[dict[str, Any]]:
    """Drop oldest turns until the wire payload fits max_context - reserve.

    Never leaves a dangling assistant tool_calls message at the boundary: the
    kept window always starts at a user message.
    """
    budget = max(1000, max_context - reserve)
    msgs = conv.messages
    start = 0
    while start < len(msgs):
        window = Conversation(conv.system_prompt, msgs[start:])
        if conversation_tokens(window) <= budget:
            break
        start += 1
        # boundary must sit on a user message so tool_call/result pairs stay intact
        while start < len(msgs) and msgs[start].role != "user":
            start += 1
    return Conversation(conv.system_prompt, msgs[start:]).wire_messages()


async def compact_history(
    conv: Conversation, provider: Any
) -> str:
    """Summarize older history via the model; replace it with one context message.

    The recent SUMMARY_KEEP_RECENT messages are preserved verbatim, extended
    back to a turn boundary so the kept window never starts with a tool result
    whose call went into the summary. Returns the summary text (empty string
    when there was nothing safely compactable).
    """
    msgs = conv.messages
    split = turn_boundary(msgs, SUMMARY_KEEP_RECENT)
    if split == 0:
        return ""
    old, recent = msgs[:split], msgs[split:]
    payload: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a precise summarizer for a coding agent."},
        *[m.to_wire() for m in old],
        {"role": "user", "content": COMPACT_PROMPT},
    ]
    text_parts: list[str] = []
    async for event in provider.stream(payload, None):
        if type(event).__name__ == "ContentDelta":
            text_parts.append(event.text)
    summary = "".join(text_parts).strip() or "(empty summary)"
    context_msg = Message(
        role="user",
        content=f"[conversation summary — earlier turns compacted]\n{summary}",
    )
    conv.messages = [context_msg, *recent]
    return summary
