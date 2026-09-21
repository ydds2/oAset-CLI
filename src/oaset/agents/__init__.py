"""Sub-agent definitions: Markdown files in ~/.oaset/agents and ./.oaset/agents.

Header (optional `key: value` lines before the first blank line):

    name: Reviewer
    description: Reviews code changes for defects
    tools: read_file, glob, grep, list_dir
    model: deepseek/deepseek-reasoner
    max_iterations: 8
    timeout_seconds: 120

Everything after the blank line is the agent's system prompt.

Scope: project agents (`./.oaset/agents`) OVERRIDE user agents of the same
name — the workspace's definition must win over a global one, the same rule
MCP servers follow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from oaset.utils import oaset_home

MAX_AGENTS = 64
SPAWN_DEPTH_LIMIT = 1  # sub-agents may not spawn further sub-agents


@dataclass
class AgentDef:
    name: str
    description: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)  # empty = all default tools
    path: Path | None = None
    model: str = ""              # "" = inherit the parent's model
    max_iterations: int = 0      # 0 = runner default
    timeout_seconds: float = 0.0  # 0 = no wall-clock limit
    # per-agent thinking depth (off/light/medium/heavy; "" = inherit the
    # parent's current /think level). Mechanical agents can pin "off"/
    # "light" and stop paying for reasoning they do not use.
    thinking: str = ""
    scope: str = ""              # "user" | "project" | "builtin"

    def allows(self, tool_name: str) -> bool:
        return not self.tools or tool_name in self.tools


BUILTIN_GENERAL = AgentDef(
    name="general",
    description="General-purpose sub-agent with all default tools",
    system_prompt=(
        "You are a focused sub-agent. Complete the given task autonomously with the "
        "available tools, then report a concise final answer (no questions back)."
    ),
    scope="builtin",
)


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_float(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _normalize_thinking(raw: str) -> str:
    """Header value → off/light/medium/heavy (aliases ok); unknown → ""."""
    from oaset.thinking import normalize_level

    return normalize_level(raw)


def _parse_agent(path: Path, scope: str = "") -> AgentDef | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    header: dict[str, str] = {}
    body = text
    match = re.search(r"\n\s*\n", text)
    head_candidate, rest = (text[: match.start()], text[match.end():]) if match else (text, "")
    looks_like_header = all(
        re.match(r"^[A-Za-z_]+\s*:", line) for line in head_candidate.splitlines() if line.strip()
    ) if head_candidate.strip() else False
    if looks_like_header:
        for line in head_candidate.splitlines():
            key, _, value = line.partition(":")
            header[key.strip().lower()] = value.strip()
        body = rest
    name = header.get("name") or path.stem
    if header.get("description"):
        description = header["description"]
    else:
        stripped = body.strip()
        description = stripped.splitlines()[0][:160] if stripped else ""
    tools = [t.strip() for t in re.split(r"[,;]", header.get("tools", "")) if t.strip()]
    prompt = body.strip() or BUILTIN_GENERAL.system_prompt
    return AgentDef(
        name=name,
        description=description,
        system_prompt=prompt,
        tools=tools,
        path=path,
        model=header.get("model", "").strip(),
        max_iterations=_parse_int(header.get("max_iterations", ""), 0),
        timeout_seconds=_parse_float(header.get("timeout_seconds", ""), 0.0),
        thinking=_normalize_thinking(header.get("thinking", "")),
        scope=scope,
    )


def agent_dirs(cwd) -> list[tuple[Path, str]]:
    """(directory, scope) in load order — project LAST so it overrides user."""
    cwd = Path(cwd)  # callers may hand a str; Path/str must never crash on `/`
    return [(oaset_home() / "agents", "user"),
            (cwd / ".oaset" / "agents", "project")]


def load_agents(cwd: Path) -> list[AgentDef]:
    """Project definitions override user definitions with the same name."""
    agents: dict[str, AgentDef] = {}
    for base, scope in agent_dirs(cwd):
        if not base.is_dir():
            continue
        for entry in sorted(base.glob("*.md")):
            if len(agents) >= MAX_AGENTS and entry.stem.lower() not in agents:
                continue
            agent = _parse_agent(entry, scope=scope)
            if agent:
                agents[agent.name.lower()] = agent  # project dir is scanned last
    return list(agents.values())


def find_agent(cwd: Path, name: str) -> AgentDef | None:
    for agent in load_agents(cwd):
        if agent.name.lower() == name.lower():
            return agent
    return None


AGENT_TEMPLATE = """name: {name}
description: {description}
tools: read_file, glob, grep, list_dir
model: {model}
max_iterations: 10
timeout_seconds: 300

You are the {name} sub-agent. Describe how this agent should work: what it
reads, what it must not touch, and what its final report must contain.
"""


def create_agent(cwd: Path, name: str, description: str = "", scope: str = "project",
                 model: str = "", overwrite: bool = False) -> Path:
    """Create <scope>/agents/<name>.md and return the path."""
    name = (name or "").strip()
    if not name:
        raise ValueError("agent name is required")
    if any(ch in name for ch in '\\/:*?"<>|'):
        raise ValueError(f"agent name contains an unsupported character: {name!r}")
    scopes = {scope_name: base for base, scope_name in agent_dirs(cwd)}
    base = scopes.get(scope)
    if base is None:
        raise ValueError(f"unknown agent scope {scope!r} (use project|user)")
    target = base / f"{name}.md"
    if target.exists() and not overwrite:
        raise ValueError(f"agent already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        AGENT_TEMPLATE.format(name=name, description=description or "TODO: describe this agent",
                              model=model),
        encoding="utf-8")
    return target


def delete_agent(cwd: Path, name: str) -> Path | None:
    agent = find_agent(cwd, name)
    if agent is None or agent.path is None or agent.scope == "builtin":
        return None
    agent.path.unlink()
    return agent.path


def agent_spawn_allowed(depth: int) -> bool:
    """Only the root agent may spawn; sub-agents cannot fan out further."""
    return depth < SPAWN_DEPTH_LIMIT
