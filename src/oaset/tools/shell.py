"""Shell execution tool: pluggable backends (local/ssh/docker), timeout with
process-tree kill, and incremental output streaming to the UI."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections import deque
from typing import Any

from oaset.i18n import t
from oaset.tools.base import EXEC, Tool, ToolContext, ToolResult
from oaset.tools.danger import classify_command
from oaset.utils import kill_tree_async, one_line, shell_command_line, truncate_text


def _resolve_sandbox(args: dict, ctx: ToolContext, backend: dict) -> tuple[bool, int]:
    """sandbox flag: explicit arg > shell.sandbox_default config (local backend only)."""
    enabled = bool(args.get("sandbox", backend.get("sandbox_default", False)))
    if enabled and str(backend.get("backend", "local")) != "local":
        enabled = False  # remote backends sandbox themselves
    memory_mb = int(backend.get("sandbox_memory_mb", 0) or 2048)
    return enabled, memory_mb

DEFAULT_TIMEOUT = 120
MAX_CAPTURE = 200_000
STREAM_CHUNK = 4096
# The stream is accumulated only to be truncated again at the end, so holding
# the WHOLE output was pure memory: a chatty command (300MB in 60s measured
# ~900MB of Python heap — list of chunks + join + decode) could OOM the agent
# while the model only ever sees MAX_CAPTURE characters. Keep head and tail.
CAPTURE_HEAD = MAX_CAPTURE // 2
CAPTURE_TAIL = MAX_CAPTURE // 4


def _escapes_workspace(command: str, cwd) -> bool:
    """Heuristic: an absolute / homedir path in a write-ish command leaves cwd."""
    import os
    import re
    from pathlib import Path

    if not command.strip():
        return False
    write_ish = re.search(
        r"(^|\s)(rm|mv|cp|copy|del|rd|rmdir|mkdir|md|echo|tee|set-content|>|>>)\b",
        command, re.IGNORECASE,
    )
    if not write_ish:
        return False
    root = Path(cwd).resolve()
    for token in re.findall(r"(?:[A-Za-z]:)?[\\/][^\s'\"|;&]+|~[^\s'\"|;&]*", command):
        raw = token.strip()
        if raw.startswith("~"):
            return True
        if raw.startswith("/") and not raw.startswith("//"):
            # POSIX absolute path, including when the host is Windows (Git Bash).
            return True
        try:
            candidate = Path(raw)
        except (OSError, ValueError):
            continue
        if not candidate.is_absolute():
            continue
        try:
            candidate.resolve().relative_to(root)
        except (ValueError, OSError):
            return True
    if os.name == "nt" and re.search(r"(^|\s)[A-Za-z]:\\", command):
        return True
    return False


def backend_command_line(command: str, backend: dict[str, Any]) -> list[str]:
    """Build argv for the configured execution backend.

    local  — the platform shell (Git Bash preferred on Windows)
    ssh    — ["ssh", <target>, command]      (remote shell parses the line)
    docker — ["docker", "exec", <container>, "bash", "-c", command]
    """
    kind = str(backend.get("backend", "local")).lower()
    if kind == "ssh":
        target = str(backend.get("ssh_target", "")).strip()
        if not target:
            raise ValueError("ssh backend requires shell.ssh_target in config.toml")
        return ["ssh", target, command]
    if kind == "docker":
        container = str(backend.get("docker_container", "")).strip()
        if not container:
            raise ValueError("docker backend requires shell.docker_container in config.toml")
        return ["docker", "exec", container, "bash", "-c", command]
    if kind == "singularity":
        image = str(backend.get("singularity_image", "")).strip()
        if not image:
            raise ValueError("singularity backend requires shell.singularity_image in config.toml")
        return ["singularity", "exec", image, "bash", "-c", command]
    if kind == "modal":
        container = str(backend.get("modal_container", "")).strip()
        if not container:
            raise ValueError("modal backend requires shell.modal_container in config.toml")
        return ["modal", "container", "exec", container, "bash", "-c", command]
    if kind == "daytona":
        target = str(backend.get("daytona_target", "")).strip()
        if not target:
            raise ValueError("daytona backend requires shell.daytona_target in config.toml")
        return ["daytona", "exec", target, "--", "bash", "-c", command]
    return shell_command_line(command)


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    await kill_tree_async(proc)


class RunShellTool(Tool):
    name = "run_shell"
    description = (
        "Run a shell command and return combined output with the exit code. Executes on the "
        "configured backend (local / ssh / docker). Timeout kills the whole process tree; "
        "output streams live into the UI."
    )
    permission = EXEC
    required = ["command"]
    parameters = {
        "command": {"type": "string", "description": "Shell command line to execute"},
        "timeout": {"type": "integer", "description": "Seconds before the process tree is killed (default 120)"},
        "sandbox": {"type": "boolean", "description": "Run in a local sandbox (job-object memory cap + orphan kill on Windows, rlimit on POSIX)"},
        "run_in_background": {"type": "boolean", "description": "Start without waiting; read later with task_output, stop with task_stop"},
    }

    def subject(self, args, ctx):
        return str(args.get("command", ""))

    def danger_level(self, args, ctx) -> str:
        """blocked/dangerous/normal — consumed by the registry dispatcher so
        a session-level 'always' can never blanket dangerous commands."""
        return classify_command(str(args.get("command", "")))

    def blocked_reason(self, args, ctx):
        verdict = classify_command(str(args.get("command", "")))
        if verdict == "blocked":
            return t("shell_blocked_dangerous")
        return None

    def permanent_allow(self, args, ctx) -> bool:
        return classify_command(str(args.get("command", ""))) == "normal"

    def gate_summary(self, args, ctx):
        backend = ctx.session_state.get("shell_config", {})
        kind = str(backend.get("backend", "local"))
        where = "" if kind == "local" else f" on {backend.get('ssh_target') or backend.get('docker_container')}"
        summary = f"run_shell[{kind}]{where}: {one_line(args['command'], 80)}"
        if classify_command(str(args.get("command", ""))) == "dangerous":
            summary += "  " + t("shell_dangerous_note")
        return summary

    async def run(self, args, ctx):
        command = str(args["command"])
        # model-controlled: without a ceiling one call could pin a child
        # for days (and yes/while-true floods burned memory unclamped)
        timeout = max(1, min(int(args.get("timeout") or DEFAULT_TIMEOUT), 3600))
        backend = ctx.session_state.get("shell_config", {}) or {}
        if str(backend.get("backend", "local")) != "local":
            from oaset.network import NetworkPolicy

            NetworkPolicy(getattr(ctx, "network_mode", None) or "pull_only").assert_allowed("remote_exec")
        sandbox_enabled, memory_mb = _resolve_sandbox(args, ctx, backend)
        if (
            str(backend.get("backend", "local")) == "local"
            and sandbox_enabled
            and _escapes_workspace(command, ctx.cwd)
        ):
            return ToolResult(
                "[workspace-write] this command looks like it writes outside the "
                "workspace (absolute path / drive letter / ~). Pass sandbox=false "
                "for this call, or keep paths inside cwd.",
                is_error=True,
            )
        try:
            argv = backend_command_line(command, backend)
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)

        kwargs: dict[str, Any] = {
            "cwd": str(ctx.cwd) if backend.get("backend", "local") == "local" else None,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
        }
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        sandbox_enabled, memory_mb = _resolve_sandbox(args, ctx, backend)
        preexec = None
        job = None
        if sys.platform == "win32":
            if sandbox_enabled:
                from oaset import sandbox as sb

                job = sb._windows_job(memory_mb)  # ONE job; the same handle is closed in finally
                # restricted-token shim: OS-level filesystem write protection
                # (deny-only group SIDs) on top of the job object's resource caps
                argv = sb.wrap_windows_command(argv)
        elif sandbox_enabled:
            from oaset import sandbox as sb

            preexec = sb.posix_preexec(memory_mb)
            if kwargs.get("cwd"):
                argv = sb.wrap_posix_command(argv, str(kwargs["cwd"]), memory_mb)
        if preexec is not None:
            kwargs["preexec_fn"] = preexec

        if args.get("run_in_background"):
            from oaset.tools.background import get_registry

            registry = get_registry(ctx)
            proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
            label = one_line(command, 60)
            if job is not None:
                from oaset import sandbox as sb

                try:
                    sb.assign_windows_job(job, proc)
                except Exception as exc:
                    await _kill_tree(proc)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    with contextlib.suppress(Exception):
                        sb.close_job(job)
                    return ToolResult(f"sandbox_unavailable: {exc}", is_error=True)
            task = registry.start_process(proc, label, job_handle=job)
            job = None  # ownership transferred: ONLY background.py closes it now
            return ToolResult(
                t("task_started", task_id=task.id, desc=label)
            )
        try:
            proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        except OSError as exc:
            if job is not None:
                from oaset import sandbox as sb

                with contextlib.suppress(Exception):
                    sb.close_job(job)
            return ToolResult(f"Failed to spawn shell: {exc}", is_error=True)
        if job is not None:
            from oaset import sandbox as sb

            try:
                sb.assign_windows_job(job, proc)
            except Exception as exc:
                await _kill_tree(proc)  # never leave a spawned process outside the job
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=5)
                with contextlib.suppress(Exception):
                    sb.close_job(job)
                return ToolResult(f"sandbox_unavailable: {exc}", is_error=True)

        sink = ctx.session_state.get("stream_sink")
        head_parts: list[bytes] = []      # first CAPTURE_HEAD bytes, verbatim
        head_size = 0
        tail_parts: deque = deque()       # last CAPTURE_TAIL bytes
        tail_size = 0
        total = 0
        timed_out = False
        started = asyncio.get_running_loop().time()
        try:
            while True:
                remaining = timeout - (asyncio.get_running_loop().time() - started)
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    piece = await asyncio.wait_for(proc.stdout.read(STREAM_CHUNK), timeout=remaining)
                except TimeoutError:
                    timed_out = True
                    break
                if not piece:
                    break
                total += len(piece)
                if head_size < CAPTURE_HEAD:
                    head_parts.append(piece)
                    head_size += len(piece)
                else:
                    tail_parts.append(piece)
                    tail_size += len(piece)
                    while tail_size > CAPTURE_TAIL:
                        tail_size -= len(tail_parts.popleft())
                if total <= MAX_CAPTURE and sink is not None:
                    try:
                        sink(piece.decode("utf-8", errors="replace"))
                    except Exception:
                        pass
        except asyncio.CancelledError:
            await _kill_tree(proc)
            raise
        finally:
            if job is not None:  # closed even on timeout/cancel/exception paths
                from oaset import sandbox as sb

                with contextlib.suppress(Exception):
                    sb.close_job(job)

        if timed_out:
            await _kill_tree(proc)  # kill BEFORE waiting: a stuck child must not hang the turn
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (TimeoutError, asyncio.CancelledError, ProcessLookupError):
                pass
            total_marker = f"\n[killed after {timeout}s timeout]".encode()
            head_parts.append(total_marker)
            exit_code = -1
        else:
            exit_code = await proc.wait()

        raw = b"".join(head_parts)
        if tail_parts:
            raw += f"\n[... {total} bytes total, middle elided ...]\n".encode()
            raw += b"".join(tail_parts)
        output = raw.decode("utf-8", errors="replace")
        if len(output) > MAX_CAPTURE:
            output = truncate_text(output, MAX_CAPTURE)
        body = output.strip() or "(no output)"
        return ToolResult(f"[exit {exit_code}]\n{body}", exit_code=exit_code)
