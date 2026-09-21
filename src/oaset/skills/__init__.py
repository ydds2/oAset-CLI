"""Skills: Markdown playbooks from the package, ~/.oaset/skills, and ./.oaset/skills.

Layout (agentskills.io): <dir>/<skill-name>/SKILL.md, first `# Title` is the
name, first plain paragraph is the description. Built-in packs ship in
``oaset.skills.builtin``; user then project skills override the same name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from oaset.utils import oaset_home

MAX_SKILLS = 64


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    scope: str = ""  # "builtin" | "user" | "project"


def builtin_skill_dir() -> Path:
    return Path(__file__).resolve().parent / "builtin"


def skill_dirs(cwd: Path) -> list[tuple[Path, str]]:
    """(directory, scope) in load order — later scopes override earlier names."""
    return [
        (builtin_skill_dir(), "builtin"),
        (oaset_home() / "skills", "user"),
        (cwd / ".oaset" / "skills", "project"),
    ]


def _parse_skill(path: Path, fallback_name: str, scope: str = "") -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    title_match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    name = title_match.group(1).strip() if title_match else fallback_name
    body_after_title = text[title_match.end():] if title_match else text
    description = ""
    for line in body_after_title.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-", "*", "```", "|")):
            if description:
                break
            continue
        description = line
        break
    return Skill(
        name=name, description=description[:200], body=text.strip(),
        path=path, scope=scope,
    )


def load_skills(cwd: Path) -> list[Skill]:
    skills: dict[str, Skill] = {}
    for base, scope in skill_dirs(cwd):
        if not base.is_dir():
            continue
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            skill = None
            if entry.is_dir():
                candidate = entry / "SKILL.md"
                if candidate.is_file():
                    skill = _parse_skill(candidate, entry.name, scope)
            elif entry.is_file() and entry.suffix == ".md" and entry.name != "SKILL.md":
                skill = _parse_skill(entry, entry.stem, scope)
            if skill is None:
                continue
            key = skill.name.lower()
            if key not in skills and len(skills) >= MAX_SKILLS:
                continue
            skills[key] = skill
    return list(skills.values())


def find_skill(cwd: Path, name: str) -> Skill | None:
    needle = name.lower()
    for skill in load_skills(cwd):
        if skill.name.lower() == needle or skill.path.parent.name.lower() == needle:
            return skill
    return None


SKILL_TEMPLATE = """# {name}

{description}

## When to use

Describe the situation this skill applies to.

## Steps

1. …
"""


def skill_scope_dir(cwd: Path, scope: str) -> Path:
    if scope == "project":
        return Path(cwd) / ".oaset" / "skills"
    if scope == "user":
        return oaset_home() / "skills"
    raise ValueError(f"unknown skill scope {scope!r} (use project|user)")


def create_skill(cwd: Path, name: str, description: str = "",
                 scope: str = "project", overwrite: bool = False) -> Path:
    """Create <scope>/skills/<name>/SKILL.md and return the file path."""
    name = (name or "").strip()
    if not name:
        raise ValueError("skill name is required")
    if any(ch in name for ch in "\\/:*?\"<>|"):
        raise ValueError(f"skill name contains an unsupported character: {name!r}")
    base = skill_scope_dir(cwd, scope)
    target = base / name / "SKILL.md"
    if target.exists() and not overwrite:
        raise ValueError(f"skill already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(SKILL_TEMPLATE.format(name=name, description=description),
                      encoding="utf-8")
    return target


def delete_skill(cwd: Path, name: str) -> Path | None:
    """Delete a skill's directory (SKILL.md layout) or its .md file."""
    import shutil

    skill = find_skill(cwd, name)
    if skill is None:
        return None
    if skill.scope == "builtin":
        raise ValueError(f"cannot delete built-in skill: {skill.name}")
    if skill.path.name == "SKILL.md":
        parent = skill.path.parent
        # only remove a directory that looks like a skill dir
        shutil.rmtree(parent)
        return parent
    skill.path.unlink()
    return skill.path
