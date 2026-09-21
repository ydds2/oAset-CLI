"""Matrix runs: N tasks, N worktrees, one command — the parallel-agent MVP.

Community signal (2026): managing parallel coding agents is its own tool
category now (ccmanager / dmux / Superset), and Gemini CLI ships per-worktree
sessions. oAset already had worktrees + subagents; this module combines them:
every task gets its own linked worktree (the user's dirty tree is never
touched), runs against the same provider/tools with its own session, and the
caller gets a review table. Worktrees are KEPT: reviewing the diff and
merging is the point.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Callable

from oaset.config import AppConfig

MatrixEvent = Callable[[str], None]


def _slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip()).strip("-")
    return (slug or "task")[:32]


def render_report(results: list[dict[str, Any]]) -> str:
    rows = ["name            status  worktree                          result"]
    for r in results:
        mark = "ok" if r["ok"] else "FAIL"
        head = " ".join(str(r["head"]).split())[:60]
        rows.append(f"{r['name']:<16}{mark:<7}{r['path'].name:<34}{head}")
    return "\n".join(rows)


async def run_matrix(
    cfg: AppConfig,
    cwd: Path,
    tasks: list[dict[str, Any]],
    model_id: str | None = None,
    max_parallel: int = 3,
    on_event: MatrixEvent = lambda _line: None,
    provider: Any | None = None,
) -> list[dict[str, Any]]:
    """Run each task in its own git worktree, at most `max_parallel` at once.

    Tasks: {"name": str, "prompt": str}. Results carry name/path/ok/head —
    the reply's first line, enough to decide what to review next.
    """
    from oaset.agent.runner import execute_prompt
    from oaset.worktree import WorktreeError, create_worktree

    if not tasks:
        return []
    semaphore = asyncio.Semaphore(max(1, max_parallel))
    results: list[dict[str, Any]] = []
    lock = asyncio.Lock()

    async def run_one(task: dict[str, Any], index: int) -> None:
        name = str(task.get("name") or f"task-{index + 1}").strip()
        prompt = str(task.get("prompt") or "").strip()
        if not prompt:
            async with lock:
                results.append({"name": name, "path": cwd, "ok": False,
                                "head": "empty prompt"})
            return
        async with semaphore:
            on_event(f"[matrix] {name}: creating worktree…")
            try:
                wt = await asyncio.to_thread(create_worktree, cwd, f"matrix-{_slug(name)}")
            except WorktreeError as exc:
                async with lock:
                    results.append({"name": name, "path": cwd, "ok": False,
                                    "head": f"worktree: {exc}"})
                on_event(f"[matrix] {name}: FAILED ({exc})")
                return
            on_event(f"[matrix] {name}: running in {wt.name}")
            try:
                reply = await execute_prompt(cfg, wt, prompt, model_id=model_id,
                                             provider=provider, yolo=True,
                                             with_mcp=False, persist=False)
                ok = True
            except Exception as exc:  # one failed task must not sink the batch
                reply, ok = f"{type(exc).__name__}: {exc}", False
            async with lock:
                results.append({
                    "name": name, "path": wt, "ok": ok,
                    "head": (reply or "").strip().splitlines()[:1][0][:120] if reply else "",
                })
            on_event(f"[matrix] {name}: {'done' if ok else 'FAILED'}")

    await asyncio.gather(*(run_one(task, i) for i, task in enumerate(tasks)))
    # stable output order regardless of completion order
    results.sort(key=lambda r: r["name"])
    return results
