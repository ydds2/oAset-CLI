"""Tool abstractions: permission levels, context, results, gates.

Permission model mirrors Kimi Code's verified behavior: read-only tools run
automatically inside the workspace; writes and shell execution require
confirmation unless the user opts into auto mode (--yolo) or allows a tool for
the session. Reads that reach outside the workspace confirm once per directory.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

READ = "read"
WRITE = "write"
EXEC = "exec"

LEVELS = {"read": READ, "write": WRITE, "exec": EXEC}


@dataclass
class ToolContext:
    cwd: Path
    mode: str = "default"  # default | plan | auto
    output_limit: int = 2000
    network_mode: str = "pull_only"  # local_only | pull_only | full
    session_state: dict[str, Any] = field(default_factory=dict)
    session_allowed: set[str] = field(default_factory=set)
    session_paths: set[str] = field(default_factory=set)  # outside-workspace dirs approved for reads
    persist_allow: Callable[[str], None] | None = None  # "always" -> durable allowlist
    rules: list[Any] = field(default_factory=list)  # PermissionRule list (config order)
    gate: Any = None  # PermissionGate: computer-use tools route approvals through it
    config: Any = None  # AppConfig: tools that need their own settings section read it here
    # audit sink (Workspace): (tool, arguments, is_error, elapsed_ms) per call
    audit: "Callable[[str, str, bool, float], None] | None" = None


@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    # Optional screenshot (PNG bytes) for computer-use tool results. The agent
    # loop attaches it as a multimodal tool message so the model can see the
    # screen; providers that cannot take images still get `output`.
    image: bytes | None = None
    image_mime: str = "image/png"
    # Process exit code for tools that run one (run_shell). The verify gate
    # reads it: a command that exited non-zero is not verification, even
    # though its output comes back as a normal (non-error) result.
    exit_code: int | None = None


class Tool(ABC):
    name: str = ""
    description: str = ""
    permission: str = READ
    # True when the tool itself dials beyond loopback (web/browser). The
    # offline-check derives its "works air-gapped" list from this flag —
    # a hand-written snapshot there used to drift as tools were added.
    network: bool = False
    parameters: dict[str, Any] = {}
    required: list[str] = []  # schema() reads this; tools with no required args need it too

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                },
            },
        }

    @abstractmethod
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...

    def gate_summary(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """One-line human description shown in the permission modal."""
        return self.name

    def checkpoint_paths(self, args: dict[str, Any], ctx: ToolContext) -> list[Path]:
        """Files whose current content should be snapshotted before this tool runs."""
        return []

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        """Human-readable preview (e.g. a diff) shown in the permission modal."""
        return None

    def in_fence(self, args: dict[str, Any], ctx: ToolContext) -> bool:
        """Whether the operation stays inside the workspace (cwd tree)."""
        return True

    def fence_paths(self, args: dict[str, Any], ctx: ToolContext) -> list[Path]:
        """Concrete paths this READ operation touches (for outside-workspace gating)."""
        return []

    def permanent_allow(self, args: dict[str, Any], ctx: ToolContext) -> bool:
        """Whether an 'always approve' may persist beyond this session.

        Default False: "always" still grants the tool for this session, but
        only tools that are safely scopeable (workspace-fenced writes, or
        shell commands classified as normal) may opt in to a durable grant.
        A durable grant is additionally bound to the approving workspace."""
        return False

    def subject(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """The operation target used by permission-rule patterns (path/command)."""
        return ""

    def subjects(self, args: dict[str, Any], ctx: ToolContext) -> list[str]:
        """Every rule-matching target this call touches (default: [subject()]).

        A call that touches SEVERAL paths must be judged per path. Judging a
        multi-file patch by its first file alone let a deny rule be bypassed by
        listing the denied path second — and an allow rule approve files the
        glob never matched.
        """
        return [self.subject(args, ctx)]


@dataclass
class PermissionRule:
    """Permission rule: pattern "Tool" or "Tool(subject-glob)"."""

    decision: str  # allow | deny | ask
    pattern: str
    scope: str = "user"  # turn-override | session-runtime | project | user
    reason: str = ""

    def matches(self, tool_name: str, subject: str) -> bool:
        from fnmatch import fnmatchcase

        pattern = self.pattern.strip()
        if "(" in pattern and pattern.endswith(")"):
            tool_glob, subject_glob = pattern[:-1].split("(", 1)
            if not fnmatchcase(tool_name, tool_glob.strip()):
                return False
            return bool(subject) and fnmatchcase(subject.replace(chr(92), "/"), subject_glob.strip().replace(chr(92), "/"))
        return fnmatchcase(tool_name, pattern)


def parse_permission_rules(raw: Any) -> list[PermissionRule]:
    rules: list[PermissionRule] = []
    if not isinstance(raw, list):
        return rules
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        decision = str(entry.get("decision", "")).lower()
        pattern = str(entry.get("pattern", "")).strip()
        if decision not in ("allow", "deny", "ask") or not pattern:
            continue
        rules.append(PermissionRule(
            decision=decision,
            pattern=pattern,
            scope=str(entry.get("scope", "user")),
            reason=str(entry.get("reason", "")),
        ))
    return rules


#: Every refusal the registry can produce for a gated call. The loop checks
#: against all of them: a headless run denies with a different prefix than an
#: interactive decline, and both must count as "not a verification".
DENIAL_PREFIXES = ("[denied by user]", "[no approver]")


def match_rules(rules: list[PermissionRule], tool_name: str, subject: str) -> str | None:
    """First matching rule wins (config order). Returns its decision or None."""
    for rule in rules:
        if rule.matches(tool_name, subject):
            return rule.decision
    return None


def combined_verdict(rules, tool, args, ctx) -> str | None:
    """Rule verdict for a call that may touch several subjects.

    Per-subject verdicts combine fail-closed: any deny wins over everything,
    any ask outranks an allow, and allow means EVERY subject matched an allow
    rule — otherwise the unmatched files fall through to the normal gate. On
    the single-subject tools (the vast majority) this is exactly match_rules.
    """
    if not rules:
        return None
    verdicts = [match_rules(rules, tool.name, subject)
                for subject in tool.subjects(args, ctx)]
    if any(v == "deny" for v in verdicts):
        return "deny"
    if any(v == "ask" for v in verdicts):
        return "ask"
    if verdicts and all(v == "allow" for v in verdicts):
        return "allow"
    return None


class PermissionGate(Protocol):
    """Async gate: resolves to 'allow' | 'always' | 'deny'."""

    async def request(self, tool_name: str, level: str, summary: str, preview: str | None = None) -> str: ...


class AutoGate:
    """Allows everything (--yolo / tests)."""

    async def request(self, tool_name: str, level: str, summary: str, preview: str | None = None) -> str:
        return "always"


# Re-exported for type hints in tools that need path helpers.


class ReadOnlyGate:
    """Allows reads, denies writes/exec (one-shot -p mode without --yolo).

    ``headless = True``: nothing was ever asked — the runner has no approver —
    so the denial must not be reported as "the user declined".
    """

    headless = True

    async def request(self, tool_name: str, level: str, summary: str, preview: str | None = None) -> str:
        return "deny"
