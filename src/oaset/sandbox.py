"""Local sandbox for shell execution (single-machine, zero-cloud).

Windows: the child process is placed in a Job Object with
  - JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE  (orphan children die with the shell)
  - JOB_OBJECT_LIMIT_PROCESS_MEMORY     (memory cap, shell.sandbox_memory_mb)
POSIX: rlimit AS cap via preexec_fn; process group kill already handled.

Enable per call with run_shell {"sandbox": true}, or globally:
    config.toml ->  [shell] sandbox_default = true
"""

from __future__ import annotations

import contextlib
import sys
from typing import Any


class SandboxUnavailable(Exception):
    pass


def _windows_job(memory_mb: int):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # ctypes.windll exists only on Windows

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperation", "WriteOperation", "OtherOperation",
            "ReadTransfer", "WriteTransfer", "OtherTransfer")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_ulonglong),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    job = kernel32.CreateJobObjectA(None, None)
    if not job:
        raise SandboxUnavailable("CreateJobObject failed")
    limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    limits.ProcessMemoryLimit = int(memory_mb) * 1024 * 1024
    if not kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation, ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        ctypes.windll.kernel32.CloseHandle(job)  # type: ignore[attr-defined]  # ctypes.windll exists only on Windows
        raise SandboxUnavailable("SetInformationJobObject failed")
    return job


def assign_windows_job(job: int, process: Any) -> int:
    """Assign `process` to an ALREADY-CREATED job `job` (one job per invocation).

    The caller creates the job once via _windows_job(), assigns the spawned
    process here, and later closes the SAME handle. Returns `job` so callers
    cannot accidentally drop the owning handle.

    asyncio.subprocess.Process (proactor loop) has no `_handle` — that private
    attribute exists only on subprocess.Popen — so the process handle is opened
    from the pid instead. Required access for AssignProcessToJobObject:
    PROCESS_SET_QUOTA | PROCESS_TERMINATE.
    """
    import ctypes

    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # ctypes.windll exists only on Windows
    proc_handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, process.pid)
    if not proc_handle:
        raise SandboxUnavailable(f"OpenProcess({process.pid}) failed")
    try:
        if not kernel32.AssignProcessToJobObject(job, proc_handle):
            raise SandboxUnavailable("AssignProcessToJobObject failed")
    finally:
        kernel32.CloseHandle(proc_handle)
    return job


def close_job(job: int) -> None:
    import ctypes

    ctypes.windll.kernel32.CloseHandle(job)  # type: ignore[attr-defined]  # ctypes.windll exists only on Windows


def posix_preexec(memory_mb: int):
    """preexec_fn for POSIX: cap address space for the child."""
    import resource

    def _apply():  # runs in the child before exec
        limit = int(memory_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return _apply


def build_posix_wrap(argv: list[str], cwd: str, platform: str, which=None) -> list[str]:
    """Pure argv transform for wrap_posix_command (platform injected so the
    Linux/macOS shapes are testable on any dev machine)."""
    from pathlib import Path

    root = str(Path(cwd).resolve())
    if platform == "linux":
        bwrap = (which or (lambda _n: None))("bwrap")
        if not bwrap:
            return argv
        return [
            bwrap, "--die-with-parent",
            "--ro-bind", "/", "/",
            "--bind", root, root,
            "--dev", "/dev", "--proc", "/proc",
            "--chdir", root,
            *argv,
        ]
    if platform == "darwin":
        seatbelt = (which or (lambda _n: None))("sandbox-exec")
        if not seatbelt:
            return argv
        profile = (
            '(version 1) (allow default) '
            '(deny file-write*) '
            f'(allow file-write* (subpath "{root}")) '
            '(allow file-write* (regex #"^/private/tmp/")) '
            '(allow file-write* (regex #"^/tmp/"))'
        )
        return [seatbelt, "-p", profile, *argv]
    return argv


def wrap_posix_command(argv: list[str], cwd: str, memory_mb: int) -> list[str]:
    """Wrap argv in bwrap (Linux) or sandbox-exec (macOS) when the binary exists.

    Falls back to the original argv so machines without those tools still run
    under rlimit-only posix_preexec.
    """
    import shutil

    return build_posix_wrap(argv, cwd, sys.platform, which=shutil.which)


def wrap_windows_command(argv: list[str]) -> list[str]:
    """Route argv through the restricted-token shim (oaset.sandboxexec).

    The shim re-derives a restricted token from its own (a duplicate of the
    caller's primary token needs no extra privilege) and starts the real
    command with CreateProcessAsUser: group-granted write ACEs stop applying,
    so writes to group-writable locations outside the workspace are denied by
    the OS. The shim sits inside the same Job Object, and jobs propagate to
    children — memory cap and kill-on-close keep covering the real command.

    Frozen exes have no python to run `-m`: they re-enter the exe through the
    `--sandbox-exec` CLI hook instead.
    """
    exe = sys.executable
    if getattr(sys, "frozen", False):
        return [exe, "--sandbox-exec", "--", *argv]
    return [exe, "-m", "oaset.sandboxexec", "--", *argv]


# keep pyflakes happy about the optional import used conditionally
_ = contextlib


def attach_current_process_to_cleanup_job() -> bool:
    """Assign THIS process to a kill-on-close job (Windows).

    Every child spawned afterwards - MCP servers, shell commands, language
    servers - then dies with this process, enforced by the kernel. This is what
    makes cleanup work even on `taskkill /F` or a closed terminal window, where
    Python-level shutdown code never executes.

    Returns True when the assignment succeeded; False elsewhere/refused.
    The job handle is intentionally kept open for the process lifetime: closing
    it is exactly the mechanism that kills the tree.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    job = kernel32.CreateJobObjectA(None, None)
    if not job:
        return False
    # KILL_ON_JOB_CLOSE only: this job guards lifetimes, not resources.
    import ctypes as _ct

    class _BASIC(_ct.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", _ct.c_longlong),
            ("PerJobUserTimeLimit", _ct.c_longlong),
            ("LimitFlags", _ct.c_ulong),
            ("MinimumWorkingSetSize", _ct.c_size_t),
            ("MaximumWorkingSetSize", _ct.c_size_t),
            ("ActiveProcessLimit", _ct.c_ulong),
            ("Affinity", _ct.c_ulonglong),
            ("PriorityClass", _ct.c_ulong),
            ("SchedulingClass", _ct.c_ulong),
        ]

    class _IO(_ct.Structure):
        _fields_ = [(n, _ct.c_ulonglong) for n in (
            "ReadOperation", "WriteOperation", "OtherOperation",
            "ReadTransfer", "WriteTransfer", "OtherTransfer")]

    class _EXTENDED(_ct.Structure):
        class _U(_ct.Union):
            _fields_ = [("Basic", _BASIC), ("Io", _IO)]
        _anonymous_ = ("u",)
        _fields_ = [("u", _U)]

    info = _EXTENDED()
    info.Basic.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, 9, _ct.byref(info), _ct.sizeof(info),
    ):
        kernel32.CloseHandle(job)
        return False
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        kernel32.CloseHandle(job)
        return False
    return True
