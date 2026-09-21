"""Background task registry: run shell commands or
subagents without blocking the turn, then list/read/stop them.

Tasks live in ToolContext.session_state["background_tasks"] — one registry
per conversation. Output is captured in a bounded deque; completion injects
a steer notification into the running loop (or surfaces on the next turn).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from oaset.utils import kill_tree_async

MAX_BUFFER = 400  # lines kept per task


@dataclass
class BackgroundTask:
    id: str
    description: str
    kind: str = "process"  # "process" | "agent"
    status: str = "running"  # running | completed | stopped | failed
    proc: Any = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    exit_code: int | None = None
    output: deque = field(default_factory=lambda: deque(maxlen=MAX_BUFFER))
    _reader: Any = None
    _coro: Any = None  # inner agent coroutine, closed defensively on stop
    job_handle: int | None = None  # Windows Job Object owning this process tree

    @property
    def elapsed(self) -> float:
        return (self.ended_at or time.time()) - self.started_at

    def summary(self) -> str:
        return f"{self.id} [{self.status}] {self.description} ({self.elapsed:.0f}s)"


class BackgroundRegistry:
    def __init__(self) -> None:
        self.tasks: dict[str, BackgroundTask] = {}
        self._agent_runners: dict[str, list[asyncio.Task]] = {}
        self._counter = 0
        self.notified: set[str] = set()

    def drain_completed(self) -> list[BackgroundTask]:
        """Finished tasks not yet reported (turn.steer notification style)."""
        done = [t for t in self.tasks.values() if t.status != "running" and t.id not in self.notified]
        self.notified.update(t.id for t in done)
        return done

    # ---------------------------------------------------------------- spawn

    def start_process(self, proc: asyncio.subprocess.Process, description: str,
                      job_handle: int | None = None) -> BackgroundTask:
        self._counter += 1
        task = BackgroundTask(id=f"task-{self._counter}", description=description, proc=proc,
                              job_handle=job_handle)
        self.tasks[task.id] = task
        task._reader = asyncio.ensure_future(self._pump(task))
        return task

    def start_agent(self, coro, description: str) -> BackgroundTask:
        """Run an async sub-agent coroutine in the background (agent-type
        background task). Its final report lands in the task output; stopping
        cancels the coroutine."""
        self._counter += 1
        task = BackgroundTask(id=f"agent-{self._counter}", description=description, kind="agent")

        async def _run() -> None:
            try:
                report = await coro
                task.output.append(str(report))
                if task.status == "running":
                    task.status = "completed"
                    task.ended_at = time.time()
            except asyncio.CancelledError:
                # if the runner was cancelled before it ever suspended on `coro`,
                # close it so Python does not emit "coroutine never awaited"
                coro.close()
                if task.status == "running":
                    task.status = "stopped"
                    task.ended_at = time.time()
                raise
            except Exception as exc:
                task.output.append(f"{type(exc).__name__}: {exc}")
                if task.status == "running":
                    task.status = "failed"
                    task.ended_at = time.time()

        self.tasks[task.id] = task
        task._coro = coro  # closed defensively in stop() if the runner never started
        runner = asyncio.ensure_future(_run())
        task._reader = runner
        self._agent_runners.setdefault(task.id, []).append(runner)
        return task

    async def _pump(self, task: BackgroundTask) -> None:
        proc = task.proc
        assert proc.stdout is not None
        try:
            async for line in proc.stdout:
                if isinstance(line, bytes):
                    # Windows pipes can yield bytes lines: rstrip on bytes
                    # raises TypeError, which the old bare except swallowed —
                    # task_output then stayed empty forever.
                    line = line.decode("utf-8", errors="replace")
                task.output.append(line.rstrip("\n\r"))
        except Exception:
            pass
        code = await proc.wait()
        task.exit_code = code
        if task.ended_at is None:
            task.ended_at = time.time()
        if task.status == "running":  # stop() owns the terminal state otherwise
            task.status = "completed" if code == 0 else f"failed ({code})"
        self._release_job(task)

    def _release_job(self, task: BackgroundTask) -> None:
        """Close the Job handle once the process tree is gone (KILL_ON_CLOSE fires here)."""
        if getattr(task, "job_handle", None) is not None:
            from oaset import sandbox as sb

            handle = int(task.job_handle)  # type: ignore[arg-type]
            try:
                sb.close_job(handle)
            except OSError:
                pass
            task.job_handle = None

    async def _kill_tree(self, proc: asyncio.subprocess.Process) -> None:
        await kill_tree_async(proc)

    # ------------------------------------------------------------ accessors

    def get(self, task_id: str) -> BackgroundTask | None:
        return self.tasks.get(task_id)

    async def stop_all(self) -> None:
        """Kill every running task — the app-shutdown path (S5 in
        docs/PLAN.zh-CN.md): process trees must not outlive the TUI."""
        for task in self.list(active_only=True, limit=200):
            with contextlib.suppress(Exception):
                await self.stop(task.id)

    def list(self, active_only: bool = False, limit: int = 20) -> list[BackgroundTask]:
        tasks = sorted(self.tasks.values(), key=lambda t: t.started_at, reverse=True)
        if active_only:
            tasks = [t for t in tasks if t.status == "running"]
        return tasks[:limit]

    async def output(self, task_id: str, block: bool = False, timeout: float = 30.0) -> str:
        task = self.tasks.get(task_id)
        if task is None:
            return f"Unknown task: {task_id}"
        if block and task.status == "running":
            await asyncio.wait_for(asyncio.shield(self._done(task)), timeout=timeout)
        tail = "\n".join(list(task.output)[-40:])
        head = f"{task.id} [{task.status}] {task.description}"
        return f"{head}\n{tail}" if tail else head

    async def _done(self, task: BackgroundTask) -> None:
        if task.kind == "agent":
            runners = self._agent_runners.get(task.id, [])
            if runners:
                await asyncio.shield(runners[0])
            return
        proc = task.proc
        if proc is not None:
            await proc.wait()

    async def stop(self, task_id: str) -> str:
        task = self.tasks.get(task_id)
        if task is None:
            return f"Unknown task: {task_id}"
        if task.status != "running":
            return f"{task.id} already {task.status}"
        task.status = "stopped"
        task.ended_at = time.time()
        if task.kind == "agent":
            for runner in self._agent_runners.get(task.id, []):
                runner.cancel()
            coro = getattr(task, "_coro", None)
            if coro is not None:
                # if the runner was cancelled before it ever started, the inner
                # coroutine was never awaited — close it to avoid RuntimeWarning
                coro.close()
                task._coro = None
            return f"{task.id} stopped (ran {task.elapsed:.0f}s)"
        proc = task.proc
        if proc is not None and proc.returncode is None:
            await self._kill_tree(proc)  # tree kill: a bare proc.kill() orphans Windows children
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                pass
        self._release_job(task)  # also fires KILL_ON_JOB_CLOSE if anything survived
        return f"{task.id} stopped (ran {task.elapsed:.0f}s)"


def get_registry(ctx: Any) -> BackgroundRegistry:
    reg = ctx.session_state.get("background_tasks")
    if reg is None:
        reg = BackgroundRegistry()
        ctx.session_state["background_tasks"] = reg
    return reg
