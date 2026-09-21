"""Git awareness: `git_status` tool (branch, staged/untracked changes).

Read-only. Returns a compact summary the model can use as working context.
Non-git directories return a graceful notice.
"""

from __future__ import annotations

import subprocess

from oaset.tools.base import READ, Tool, ToolContext, ToolResult


def _git(args: list[str], cwd, timeout: float = 8.0) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # NOTE: keep leading spaces — porcelain status columns start with them
    return result.stdout.rstrip("\r\n")


class GitStatusTool(Tool):
    name = "git_status"
    description = (
        "Git working-tree summary for the current workspace: branch, staged/untracked "
        "files, dirty count. Empty result means the directory is not a git repository."
    )
    permission = READ
    required = []
    parameters = {}

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], ctx.cwd)
        if branch is None:
            return ToolResult("Not a git repository.")
        porcelain = _git(["status", "--porcelain"], ctx.cwd) or ""
        entries: list[tuple[str, str]] = []
        for line in porcelain.splitlines():
            if len(line) < 4:
                continue
            entries.append((line[:2], line[3:].strip()))
        staged = [path for xy, path in entries if xy[0] != " "]
        dirty = [path for xy, path in entries if xy[0] == " " and xy[1] != " "]
        untracked = [path for xy, path in entries if xy == "??"]

        summary = [f"branch: {branch}"]
        if staged:
            summary.append(f"staged ({len(staged)}): " + "; ".join(staged[:8]))
        if dirty:
            summary.append(f"unstaged changes ({len(dirty)}): " + "; ".join(dirty[:8]))
        if untracked:
            summary.append(f"untracked ({len(untracked)}): " + "; ".join(untracked[:8]))
        if not (staged or dirty or untracked):
            summary.append("working tree clean")
        recent = _git(["log", "--oneline", "-5"], ctx.cwd)
        if recent:
            summary.append("recent commits:\n" + recent)
        return ToolResult("\n".join(summary))
