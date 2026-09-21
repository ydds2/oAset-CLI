"""Agent memory (closed learning loop, local edition).

- ~/.oaset/memory/MEMORY.md  — durable facts about the machine/projects/workflows
- ~/.oaset/memory/USER.md    — deepening model of the user (preferences, context)

Both are injected into the system prompt at session start (truncated). The agent
curates them autonomously through the `memory` tool; the prompt carries a nudge
to persist important knowledge ("memory nudges").
"""

from __future__ import annotations

from pathlib import Path

from oaset.tools.base import WRITE, Tool, ToolContext, ToolResult
from oaset.utils import oaset_home, truncate_text

MEMORY_LIMIT = 6000  # chars injected per file
MAX_MEMORY_FILES = 2


def memory_dir(home: Path | None = None) -> Path:
    base = (home or oaset_home()) / "memory"
    base.mkdir(parents=True, exist_ok=True)
    return base


def memory_path(scope: str, home: Path | None = None) -> Path:
    name = "USER.md" if scope == "user" else "MEMORY.md"
    return memory_dir(home) / name


def read_memory(scope: str, home: Path | None = None) -> str:
    path = memory_path(scope, home)
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def write_memory(scope: str, content: str, home: Path | None = None, append: bool = False) -> str:
    from oaset.utils import atomic_write_text, file_lock

    path = memory_path(scope, home)
    # read-modify-write under one lock: two sessions appending was a
    # lost-update race, and a crash mid-write truncated durable memory
    with file_lock(path):
        if append:
            existing = read_memory(scope, home)
            content = (existing + "\n" + content).strip() if existing else content
        atomic_write_text(path, content.strip() + "\n")
    return str(path)


def load_memory_context(home: Path | None = None) -> str:
    """Both memory files, ready for prompt injection."""
    sections = []
    for scope, title in (("global", "Durable memory (machine/projects/workflows)"), ("user", "User model")):
        text = read_memory(scope, home)
        if text:
            sections.append(f"### {title}\n{truncate_text(text, MEMORY_LIMIT)}")
    if not sections:
        return ""
    return "\n\n".join(sections)


class MemoryTool(Tool):
    name = "memory"
    description = (
        "Persist durable knowledge across sessions. scope='global' stores facts about "
        "this machine/projects/workflows; scope='user' stores what you learn about the "
        "user (preferences, role, ongoing goals). Use append=true to add a line."
    )
    # persists to global ~/.oaset/MEMORY.md / USER.md — that is a mutation of
    # user-level state, not a read; it goes through approval like any write
    permission = WRITE
    required = ["content"]
    parameters = {
        "scope": {"type": "string", "description": "global | user (default global)"},
        "content": {"type": "string", "description": "Memory text to store (markdown bullet)"},
        "append": {"type": "boolean", "description": "Append instead of replace (default true)"},
    }

    def in_fence(self, args: dict, ctx: ToolContext) -> bool:
        # MEMORY.md / USER.md live under ~/.oaset, not the workspace: the gate
        # must label the call OUTSIDE so it asks every time and can never grow
        # a durable "always" grant for overwriting durable user state
        return False

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        scope = "user" if str(args.get("scope", "global")).lower() == "user" else "global"
        content = str(args.get("content", "")).strip()
        if not content:
            return ToolResult("content must not be empty.", is_error=True)
        append = args.get("append", True)
        path = write_memory(scope, content, append=bool(append))
        preview = truncate_text(content, 120)
        return ToolResult(f"Memory saved to {path} ({scope}): {preview}")
