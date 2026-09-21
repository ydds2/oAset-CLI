"""Git worktree isolation for a coding session.

Creates `git worktree add` under ``.oaset/worktrees/<name>`` so the agent
edits a linked checkout instead of the user's dirty tree. Remove deletes
the worktree and the branch created for it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from oaset.utils import oaset_home


class WorktreeError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
    )


def is_git_repo(cwd: Path) -> bool:
    try:
        result = _git(["rev-parse", "--is-inside-work-tree"], cwd)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def worktree_root(cwd: Path) -> Path:
    return Path(cwd) / ".oaset" / "worktrees"


def create_worktree(cwd: Path, name: str, *, branch: str | None = None) -> Path:
    """Add a linked worktree. Returns the new checkout path."""
    name = (name or "").strip()
    if not name or any(ch in name for ch in r'\/:*?"<>|'):
        raise WorktreeError("worktree name is required and cannot contain path separators")
    if not is_git_repo(cwd):
        raise WorktreeError("not a git repository")
    dest = worktree_root(cwd) / name
    if dest.exists():
        raise WorktreeError(f"worktree already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    branch = branch or f"oaset/{name}"
    # Prefer a new branch off HEAD so the user's current branch is untouched.
    result = _git(["worktree", "add", "-b", branch, str(dest), "HEAD"], cwd)
    if result.returncode != 0:
        raise WorktreeError((result.stderr or result.stdout or "git worktree add failed").strip())
    return dest


def list_worktrees(cwd: Path) -> list[dict[str, str]]:
    if not is_git_repo(cwd):
        return []
    try:
        result = _git(["worktree", "list", "--porcelain"], cwd)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            if current:
                rows.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        rows.append(current)
    return rows


def remove_worktree(cwd: Path, name: str) -> Path:
    name = (name or "").strip()
    # same validation as create: without it "../.." addressed any registered
    # worktree of the repo
    if not name or any(ch in name for ch in r'\/:*?"<>|'):
        raise WorktreeError("worktree name is required and cannot contain path separators")
    dest = worktree_root(cwd) / name
    if not dest.exists():
        raise WorktreeError(f"worktree not found: {dest}")
    result = _git(["worktree", "remove", "--force", str(dest)], cwd)
    if result.returncode != 0:
        raise WorktreeError((result.stderr or result.stdout or "git worktree remove failed").strip())
    # The docstring promised "deletes the worktree AND the branch": without
    # this, oaset/<name> branches piled up on every create/remove cycle.
    # Best-effort: a branch still merged elsewhere must not fail the removal.
    _git(["branch", "-D", f"oaset/{name}"], cwd)
    return dest


def scratch_dir() -> Path:
    """Non-git fallback: a directory under OASET_HOME, never the project."""
    path = oaset_home() / "worktrees"
    path.mkdir(parents=True, exist_ok=True)
    return path
