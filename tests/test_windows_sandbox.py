"""Real-side-effect Windows sandbox tests (SAN-01/SAN-02).

These spawn actual processes and verify Job ownership, tree kill on timeout,
memory cap enforcement and background job cleanup — they are skipped off
Windows because the semantics only exist there.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from oaset.tools.base import ToolContext
from oaset.tools.shell import RunShellTool

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object semantics")


@pytest.fixture(autouse=True)
def _require_restricted_spawn(request):
    """Hosted runners cannot build restricted tokens; skip with the probe's
    reason instead of failing on infrastructure (verified on real Windows)."""
    reason = request.getfixturevalue("restricted_spawn_unavailable_reason")
    if reason:
        pytest.skip(reason)


def _ctx(tmp_path: Path, memory_mb: int = 2048) -> ToolContext:
    ctx = ToolContext(cwd=tmp_path, mode="auto", output_limit=100_000)
    ctx.session_state["shell_config"] = {
        "backend": "local",
        "sandbox_default": True,
        "sandbox_memory_mb": memory_mb,
    }
    return ctx


def _pid_alive(pid: int) -> bool:
    import ctypes

    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _job_pids(job: int) -> list[int]:
    """List the process ids owned by a Job (JOBOBJECT_BASIC_PROCESS_ID_LIST)."""
    import ctypes

    class IDLIST(ctypes.Structure):
        _fields_ = [("NumberOfAssignedProcesses", ctypes.c_ulong),
                    ("NumberOfProcessIdsInList", ctypes.c_ulong),
                    ("ProcessIdList", ctypes.c_ulong * 64)]

    info = IDLIST()
    ok = ctypes.windll.kernel32.QueryInformationJobObject(
        job, 3, ctypes.byref(info), ctypes.sizeof(info), None)
    return list(info.ProcessIdList[: info.NumberOfProcessIdsInList]) if ok else []


def test_job_object_actually_owns_the_spawned_process():
    """Assign a live process and query the Job: the pid must appear in the list."""
    from oaset import sandbox as sb

    job = sb._windows_job(2048)
    try:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            sb.assign_windows_job(job, proc)
            assert proc.pid in _job_pids(job)
        finally:
            proc.kill()
            proc.wait(timeout=5)
    finally:
        sb.close_job(job)


async def test_timeout_kills_grandchild_process_tree(tmp_path: Path):
    """sandbox=True + timeout must kill the whole tree, not just the direct child."""
    pid_file = tmp_path / "grandchild.pid"
    grandchild = tmp_path / "grandchild.py"
    spawner = tmp_path / "spawner.py"
    grandchild.write_text(
        "import os, sys, time\n"
        "with open(sys.argv[1], 'w') as fh:\n"
        "    fh.write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    spawner.write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "print('child started', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    res = await RunShellTool().run(
        {"command": f'"{sys.executable}" "{spawner}" "{grandchild}" "{pid_file}"',
         "sandbox": True, "timeout": 3}, _ctx(tmp_path))
    assert "[exit -1]" in res.output, f"expected timeout kill, got: {res.output[:200]}"

    deadline = time.time() + 10
    while not pid_file.exists() and time.time() < deadline:
        await asyncio.sleep(0.2)
    assert pid_file.exists(), "grandchild never wrote its pid"
    grandchild_pid = int(pid_file.read_text().strip())
    deadline = time.time() + 10
    while _pid_alive(grandchild_pid) and time.time() < deadline:
        await asyncio.sleep(0.2)
    assert not _pid_alive(grandchild_pid), "grandchild survived the sandboxed timeout kill"


async def test_sandbox_memory_cap_kills_allocation(tmp_path: Path):
    """A 256MB cap must stop a 512MB allocation (memory limit is enforced by the Job)."""
    script = "b = bytearray(512 * 1024 * 1024); print('allocated', len(b), flush=True)"
    res = await RunShellTool().run(
        {"command": f'"{sys.executable}" -c "{script}"', "sandbox": True, "timeout": 30},
        _ctx(tmp_path, memory_mb=256))
    assert "allocated" not in res.output
    assert "[exit 0]" not in res.output or "MemoryError" in res.output, (
        "the cap must refuse the allocation (nonzero exit or MemoryError)"
    )


async def test_background_task_stores_and_releases_job_handle(tmp_path: Path):
    """run_in_background + sandbox must bind the Job and close it on completion."""
    from oaset.tools.background import get_registry

    ctx = _ctx(tmp_path)
    started = await RunShellTool().run(
        {"command": f'"{sys.executable}" -c "import time; time.sleep(2)"',
         "sandbox": True, "run_in_background": True}, ctx)
    assert not started.is_error, started.output
    registry = get_registry(ctx)
    task_id = started.output.split("task-")[1].split()[0] if "task-" in started.output else ""
    task = registry.get(f"task-{task_id}") or registry.list(limit=1)[0]
    assert task.job_handle is not None, "background sandbox must hold the job handle"
    await asyncio.sleep(3)  # let it finish
    assert task.status != "running"
    assert task.job_handle is None, "job handle must be released after completion"


# ------------------------------------------- restricted-token filesystem guard
#
# sandbox=True now routes every local Windows command through the
# oaset.sandboxexec shim: a deny-only restricted token (Authenticated Users /
# Administrators) swapped into the suspended child. The tests below prove the
# OS actually enforces it on THIS machine: workspace writes pass, writes whose
# ONLY grant goes through a denied group are refused by the kernel.


def _sbx_run(argv, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "oaset.sandboxexec", "--", *argv],
        capture_output=True, timeout=120, cwd=cwd,
    )


def test_restricted_shim_runs_and_propagates_exit_code(tmp_path: Path):
    r = _sbx_run(["cmd", "/c", "echo probe & exit /b 7"], cwd=tmp_path)
    assert b"probe" in r.stdout, r.stderr[:200]
    assert r.returncode == 7


def test_restricted_write_allowed_inside_workspace(tmp_path: Path):
    target = tmp_path / "ok.txt"
    r = _sbx_run(["cmd", "/c", "echo w> " + str(target)], cwd=tmp_path)
    assert target.exists(), "workspace writes must keep working (user SID stays enabled)"
    assert r.returncode == 0


def test_restricted_write_denied_for_group_only_grant(tmp_path: Path):
    """A dir granted ONLY to Authenticated Users (owner/systadmin ACEs removed):
    the normal token writes through the group; the restricted child must be
    refused BY THE OS (deny-only SID removes the group grant)."""
    import os

    target = tmp_path / "au"
    target.mkdir()
    try:
        for args in (
            ["icacls", str(target), "/inheritance:r"],
            ["icacls", str(target), "/remove:g", "SYSTEM", "Administrators", "OWNER RIGHTS"],
            ["icacls", str(target), "/grant:r", "Authenticated Users:(OI)(CI)F"],
        ):
            subprocess.run(args, check=True, capture_output=True)
        marker = target / "x.txt"
        subprocess.run(["cmd", "/c", "echo x> " + str(marker)], capture_output=True)
        assert marker.exists(), "control (unrestricted) must write via the group ACE"
        marker.unlink()
        r = _sbx_run(["cmd", "/c", "echo x> " + str(marker)], cwd=tmp_path)
        assert not marker.exists(), "the OS must have denied the restricted write"
        assert r.returncode != 0
    finally:
        # pytest must still be able to delete the tree afterwards
        subprocess.run(["icacls", str(target), "/grant", "*S-1-1-0:(OI)(CI)F"],
                       capture_output=True)
        os.chmod(target, 0o777)


async def test_run_shell_sandbox_end_to_end(tmp_path: Path):
    """run_shell sandbox=True must go through the shim and keep the same
    workspace semantics: in-cwd write fine, group-only-grant write refused."""
    import os

    guarded = tmp_path / "au"
    guarded.mkdir()
    try:
        for args in (
            ["icacls", str(guarded), "/inheritance:r"],
            ["icacls", str(guarded), "/remove:g", "SYSTEM", "Administrators", "OWNER RIGHTS"],
            ["icacls", str(guarded), "/grant:r", "Authenticated Users:(OI)(CI)F"],
        ):
            subprocess.run(args, check=True, capture_output=True)
        marker = guarded / "via-shell.txt"
        await RunShellTool().run(
            {"command": f'"{sys.executable}" -c "open(r\'{marker}\', \'w\')"',
             "sandbox": True, "timeout": 60}, _ctx(tmp_path))
        assert not marker.exists(), "sandboxed run_shell must not write a group-only dir"
        inside = tmp_path / "inside.txt"
        allowed = await RunShellTool().run(
            {"command": f'"{sys.executable}" -c "open(r\'{inside}\', \'w\').write(\'x\')"',
             "sandbox": True, "timeout": 60}, _ctx(tmp_path))
        assert inside.exists(), allowed.output[:200]
    finally:
        subprocess.run(["icacls", str(guarded), "/grant", "*S-1-1-0:(OI)(CI)F"],
                       capture_output=True)
        os.chmod(guarded, 0o777)
