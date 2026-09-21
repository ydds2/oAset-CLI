"""`skill_create` tool — the curator half of the learning loop.

After solving a complex multi-step task the agent can persist the approach as a
reusable Markdown skill (autonomous skill creation, agentskills.io layout).
"""

from __future__ import annotations

import re
from typing import Any

from oaset.skills import load_skills
from oaset.tools.base import WRITE, Tool, ToolContext, ToolResult
from oaset.utils import oaset_home, truncate_text


def _slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_\-]+", "-", name.strip()).strip("-").lower()
    return slug or "skill"


class SkillCreateTool(Tool):
    name = "skill_create"
    description = (
        "Create or update a reusable skill pack (Markdown) after solving a complex task, "
        "so future sessions can reuse the approach. Skills live in ~/.oaset/skills/<slug>/SKILL.md."
    )
    # creates/overwrites ~/.oaset/skills/<slug>/SKILL.md — a global-state
    # mutation, approved like any other write
    permission = WRITE
    required = ["name", "description", "body"]
    parameters = {
        "name": {"type": "string", "description": "Short skill name, e.g. 'django-migrations'"},
        "description": {"type": "string", "description": "One-line description of when to use it"},
        "body": {"type": "string", "description": "Full skill instructions in Markdown"},
    }

    def in_fence(self, args: dict[str, Any], ctx: ToolContext) -> bool:
        # skills live under ~/.oaset/skills — outside the workspace, so the
        # approval must say so and cannot be persisted into a blanket grant
        return False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name", "")).strip()
        description = str(args.get("description", "")).strip()
        body = str(args.get("body", "")).strip()
        if not name or not body:
            return ToolResult("name and body are required.", is_error=True)
        slug = _slugify(name)
        skills_dir = oaset_home() / "skills" / slug
        skills_dir.mkdir(parents=True, exist_ok=True)
        path = skills_dir / "SKILL.md"
        updated = path.exists()
        path.write_text(
            f"# {name}\n\n{description}\n\n{body}\n", encoding="utf-8"
        )
        count = len(load_skills(ctx.cwd))
        verb = "updated" if updated else "created"
        return ToolResult(
            truncate_text(
                f"Skill {verb}: {path} (now {count} skills). "
                f"Call read_file on {path} before using it; "
                f"or /skills {name} to pin it for this session.",
                ctx.output_limit,
            )
        )
