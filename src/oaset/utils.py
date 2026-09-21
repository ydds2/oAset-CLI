"""Shared utilities: paths, ids, truncation, token estimation, shell detection."""

from __future__ import annotations

import contextlib
import datetime as dt
import functools
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

# asyncio is deliberately NOT imported at module level: it costs ~180ms on
# Windows (ssl/logging/windows_events) and utils is on every cold start,
# including `oaset --version`. The async helpers import it at call time —
# the TUI pays for it anyway via textual, quick CLI paths never do.

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
# East-Asian wide + emoji presentation: enough for our CJK titles without
# importing rich at module load (config/cli import utils on every start).
_WIDE_RE = re.compile(
    r"[\u1100-\u115F\u2329-\u232A\u2E80-\uA4CF\uAC00-\uD7A3"
    r"\uF900-\uFAFF\uFE10-\uFE19\uFE30-\uFE6F\uFF00-\uFF60\uFFE0-\uFFE6"
    r"\U0001F300-\U0001FAFF]"
)


def cell_width(text: str) -> int:
    """Display cells for `text` (ASCII=1, CJK/emoji=2). No rich import on ASCII."""
    if not text:
        return 0
    if len(text) == 1:
        return 2 if _WIDE_RE.match(text) else 1
    if _WIDE_RE.search(text) is None:
        return len(text)
    return sum(2 if _WIDE_RE.match(ch) else 1 for ch in text)


# ------------------------------------------------------------- process trees


def kill_tree_sync(pid: int) -> None:
    """Force-kill a process and its whole tree.

    Windows uses ``taskkill /F /T`` via argv — no cmd.exe interpretation,
    unlike the old ``os.system("taskkill ... >nul 2>&1")``.
    POSIX kills the process group.
    """
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=10)
    else:
        import signal

        os.killpg(os.getpgid(pid), signal.SIGKILL)


async def kill_tree_async(proc) -> None:
    """Awaitable tree kill: the kill command runs in a worker thread so a slow
    ``taskkill`` can never block the UI event loop (S2 in docs/PLAN.zh-CN.md)."""
    import asyncio

    if proc.returncode is not None:
        return
    try:
        await asyncio.to_thread(kill_tree_sync, proc.pid)
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


# ---------------------------------------------------------- ordered IO thread

_io_executor: ThreadPoolExecutor | None = None
# Single slot: when the FIFO worker started its CURRENT operation (monotonic).
# Empty list = idle. Written only from the IO worker thread itself.
_io_op_started: list[float] = []

IO_BARRIER_TIMEOUT = 10.0  # seconds before a full queue is declared stuck


class IoQueueBlockedError(RuntimeError):
    """The ordered-IO worker has not drained within its budget.

    Typical causes on Windows: an antivirus scan holding the transcript file,
    a sync client (OneDrive/Dropbox) locking writes, or an unreachable
    network drive inside the session paths. Callers must surface this
    instead of waiting forever — an invisible hang IS a frozen TUI to the
    user."""


def _io_stall_seconds() -> float:
    """How long the operation currently running on the IO thread has lasted."""
    return time.monotonic() - _io_op_started[0] if _io_op_started else 0.0


def _io_tracked(func: Callable[[], Any]) -> Callable[[], Any]:
    def wrapper() -> Any:
        _io_op_started.clear()
        _io_op_started.append(time.monotonic())
        try:
            return func()
        finally:
            _io_op_started.clear()
    return wrapper


def _io_pool() -> ThreadPoolExecutor:
    global _io_executor
    if _io_executor is None:
        # A single worker makes run_in_executor FIFO, so session-file appends
        # keep their original order even though they no longer run inline.
        _io_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oaset-io")
    return _io_executor


def run_io(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Awaitable that runs blocking file I/O on the ordered IO thread.

    The event loop never blocks on disk (S2 in docs/PLAN.zh-CN.md) while the
    one-worker pool keeps transcript/checkpoint writes strictly ordered."""
    import asyncio

    loop = asyncio.get_running_loop()
    return loop.run_in_executor(
        _io_pool(), _io_tracked(functools.partial(func, *args, **kwargs)))


async def io_barrier(timeout: float = IO_BARRIER_TIMEOUT) -> None:
    """Resolve once every run_io task submitted so far has completed.

    Single-worker FIFO makes this a sequencing primitive: a destructive
    session mutation (`/rewind`, `/undo`, delete) awaits the barrier first so
    queued appends can never land after a truncation.

    BOUNDED: if the queue has not drained within `timeout`, raises
    IoQueueBlockedError instead of hanging forever — one wedged disk write
    (AV lock, sync client, dead network drive) must not turn every session
    operation into an invisible freeze."""
    import asyncio

    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(_io_pool(), _io_tracked(lambda: None)), timeout)
    except TimeoutError as exc:
        raise IoQueueBlockedError(
            f"io queue did not drain within {timeout:.0f}s; "
            f"current operation has been running for {_io_stall_seconds():.0f}s"
        ) from exc


# ---------------------------------------------------------------- filesystem


def oaset_home() -> Path:
    """oAset data home (~/.oaset), relocatable via OASET_HOME."""
    env = os.environ.get("OASET_HOME")
    base = Path(env).expanduser() if env else Path.home() / ".oaset"
    base.mkdir(parents=True, exist_ok=True)
    return base


def sessions_dir() -> Path:
    p = oaset_home() / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = oaset_home() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def workdir_bucket(cwd: Path) -> str:
    """Bucket name for a working directory: wd_<sanitized-name>_<hash8>.

    Mirrors the per-workdir session grouping verified in ~/.kimi-code/sessions.
    """
    name = re.sub(r"[^A-Za-z0-9_\-]+", "_", cwd.name).strip("_")[:24] or "root"
    digest = hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()[:8]
    return f"wd_{name}_{digest}"


def expand_path(p: str | Path, cwd: Path) -> Path:
    path = Path(str(p)).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return Path(os.path.normpath(path))


def short_path(p: str | Path, cwd: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(cwd))
    except Exception:
        return str(p)


def find_project_context(cwd: Path, max_chars: int = 8000) -> str | None:
    """Locate project context (OASET.md > AGENTS.md > CLAUDE.md), nearest directory wins."""
    names = ("OASET.md", "AGENTS.md", "CLAUDE.md")
    for directory in (cwd, *cwd.parents):
        for name in names:
            candidate = directory / name
            try:
                if candidate.is_file():
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    return truncate_text(text.strip(), max_chars)
            except OSError:
                continue
    return None


# ---------------------------------------------------------------- ids & time


def now_iso() -> str:
    """UTC timestamp with microsecond precision (stable ordering within a second)."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


# ------------------------------------------------------- cross-process locks


@contextlib.contextmanager
def file_lock(target, timeout: float = 5.0, poll: float = 0.02):
    """Exclusive cross-process lock on a sidecar ``<target>.lock`` file.

    Local-first multi-process safety: a TUI, an ACP server and a one-shot
    CLI can share ``~/.oaset`` at the same time, and every shared write
    (transcripts, the session index, credentials, config) goes through one
    of these. msvcrt.locking on Windows, flock elsewhere; no third-party
    dependency. Contention past `timeout` raises TimeoutError — callers who
    would rather degrade than block should catch it.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    fh = open(lock_path, "a+b")  # noqa: SIM115 — held for the context duration
    import time as _time

    deadline = _time.monotonic() + timeout
    locked = False
    try:
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if _time.monotonic() >= deadline:
                    raise TimeoutError(f"file lock busy: {lock_path}") from None
                _time.sleep(poll)
        yield lock_path
    finally:
        if locked:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def atomic_write_text(path, text: str) -> None:
    """Write `text` so a crash never leaves a half-written file.

    Windows: `os.replace` is REFUSED while any other process holds the
    target open for reading (WinError 5) — every lock-free reader (session
    loads, config loads) makes the replace fail spuriously. A short retry
    absorbs those windows instead of surfacing user-visible write errors
    for data that was never in danger.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
    tmp.write_text(text, encoding="utf-8")
    import time as _time

    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            _time.sleep(0.02 * (attempt + 1))


# ---------------------------------------------------------------- text


def jsonable(value: Any, _depth: int = 0) -> Any:
    """Best-effort JSON-safe view of a value (live objects described, not dropped).

    Event payloads legitimately carry live objects — a ``ProviderError``, an
    assistant ``Message``, a ``ToolResult`` holding PNG bytes. Serializers that
    raise on those lose whole frames, so anything that is not a plain JSON
    value is described instead (an image blob is reported by size, never
    inlined; depth-capped so a reference loop terminates).
    """
    import contextlib
    import dataclasses

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"bytes": len(value)}
    if isinstance(value, dict):
        return {str(key): jsonable(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item, _depth + 1) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: jsonable(getattr(value, f.name), _depth + 1)
                for f in dataclasses.fields(value)}
    if isinstance(value, BaseException):
        payload: dict[str, Any] = {"error": type(value).__name__,
                                   "message": str(value)}
        hint = getattr(value, "hint", None)
        if callable(hint):
            with contextlib.suppress(Exception):
                payload["hint"] = str(hint())
        return payload
    return str(value)


def truncate_text(text: str, limit: int) -> str:
    """Truncate long text keeping head and tail, with an explicit marker."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n...[{omitted} chars truncated]...\n{text[-tail:]}"


def estimate_tokens(text: str) -> int:
    """Cheap token heuristic: CJK chars ~1 token each, other text ~4 chars/token."""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def display_tool_name(name: str) -> str:
    """User-facing tool label: web_search → WebSearch, web_fetch → FetchURL."""
    from oaset.i18n import t

    native = t("tool_native_suffix")
    aliases = {
        "web_search": "WebSearch",
        "web_fetch": "FetchURL",
        # provider-native skills: show the channel, not the raw $-name
        "$web_search": "WebSearch" + native,
    }
    raw = name or ""
    if raw in aliases:
        return aliases[raw]
    if raw.startswith("$"):
        return raw[1:].replace("_", " ").title() + native
    return "".join(part[:1].upper() + part[1:] for part in raw.split("_") if part) or raw


def one_line(text: str, limit: int = 80) -> str:
    """Single line, cut to `limit` DISPLAY CELLS with an ellipsis.

    len() was wrong for CJK/emoji: a 40-character Chinese title is 80 cells
    wide, so a len()-based cut at 80 produced a line that still overflowed the
    terminal and wrapped (or got clipped) — the old "peek is one line"
    promise broke on exactly the content this product is used with.
    """
    line = " ".join(text.split())
    if cell_width(line) <= limit:
        return line
    budget = max(1, limit - 1)  # reserve the ellipsis cell
    out: list[str] = []
    used = 0
    for ch in line:
        width = cell_width(ch)
        if used + width > budget:
            break
        out.append(ch)
        used += width
    return "".join(out) + "…"


def write_side_output(out_dir: str, name: str, text: str) -> str:
    """Persist oversized tool output next to the transcript; returns the path.

    Runs on the ordered IO thread (call it through run_io). This is what makes
    truncation recoverable: the marker in the truncated text points at a file
    the model can read back with read_file(offset, limit)."""
    side_dir = Path(out_dir)
    side_dir.mkdir(parents=True, exist_ok=True)
    seq = len(list(side_dir.glob("*.txt"))) + 1
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:32] or "tool"
    # glob-count names collide under parallel dispatch (both writers computed
    # the same seq and the later overwrote the earlier) — a uuid suffix makes
    # every spill unique while keeping the sortable prefix
    side = side_dir / f"{seq:03d}-{safe}-{uuid.uuid4().hex[:6]}.txt"
    side.write_text(text, encoding="utf-8")
    return str(side)


def fmt_bytes(size: int) -> str:
    """Compact human byte size for pickers and notices (1.4 MB / 812 KB / 73 B)."""
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def workspace_files(cwd: Path, query: str = "", limit: int = 30,
                    suffixes: tuple[str, ...] | list[str] = ()) -> list[str]:
    """Relative file paths under cwd matching query (substring), pruned walk.

    `suffixes` filters DURING the walk, before `limit` is applied. Filtering a
    already-truncated list is wrong: a workspace with hundreds of source files
    would exhaust the limit long before the walk reached its few images, so the
    picker showed "no files" for a directory that does contain them.
    """
    skip = {".git", ".oaset", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".idea", ".vscode"}
    root = Path(cwd)
    wanted = tuple(s.lower() for s in suffixes if s)
    # a suffix-filtered walk has to look at many more entries per hit
    scan_budget = limit * (20 if wanted else 6)
    results: list[str] = []
    stack = [root]
    scanned = 0
    while stack and scanned < scan_budget:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name.lower())
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned > scan_budget:
                break
            if entry.name in skip or entry.name.startswith("."):
                continue
            rel = Path(entry.path).relative_to(root).as_posix()
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                lowered = rel.lower()
                if query in lowered and (not wanted or lowered.endswith(wanted)):
                    results.append(rel)
                    if len(results) >= limit:
                        return results
    return results


# ---------------------------------------------------------------- attachments

IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp"}
IMAGE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:)?[^\s'\"]+\.(" + "|".join(ext.strip(".") for ext in IMAGE_EXTS) + r")\b",
    re.IGNORECASE,
)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 4


FALLBACK_ENCODINGS = ("utf-8", "gbk", "gb18030", "big5", "latin-1")


def read_text_smart(path: Path, max_bytes: int | None = None) -> tuple[str, str]:
    """Read text trying utf-8 then common Chinese Windows encodings.

    Returns (text, encoding). Raises the last UnicodeDecodeError when nothing
    decodes (binary files never reach here — callers pre-check).
    """
    raw = path.read_bytes() if max_bytes is None else path.read_bytes()[:max_bytes]
    last_error: Exception | None = None
    for enc in FALLBACK_ENCODINGS:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError) as exc:
            last_error = exc
    raise last_error or UnicodeDecodeError("utf-8", raw, 0, 1, "undecodable")


def write_text_smart(path: Path, text: str, encoding: str | None = None) -> str:
    """Write text; keeps the file's original encoding when known, else utf-8.

    Line endings follow the file's existing style (CRLF preserved on Windows
    files) so round-trips never rewrite whole files with different bytes style.
    """
    raw = path.read_bytes() if path.exists() else b""
    crlf = b"\r\n" in raw[:4096] if raw else (os.name == "nt")
    enc = encoding
    if enc is None:
        if raw:
            for candidate in FALLBACK_ENCODINGS:
                try:
                    raw.decode(candidate)
                    enc = candidate
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
        enc = enc or "utf-8"
    data = text.encode(enc, errors="replace")
    if crlf and "\r\n" not in text:
        data = data.replace(b"\n", b"\r\n")
    path.write_bytes(data)
    return enc


def image_path_to_part(path: Path) -> dict | None:
    """Encode one image file as an OpenAI image_url part; None if unreadable."""
    import base64

    ext = path.suffix.lower()
    mime = IMAGE_EXTS.get(ext)
    if mime is None or not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_IMAGE_BYTES:
            return None
        data = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def extract_image_attachments(text: str, cwd: Path, enabled: bool = True) -> list[dict]:
    """Find image paths mentioned in the text and build OpenAI image_url parts."""
    if not enabled:
        return []
    parts: list[dict] = []
    seen: set[str] = set()
    for match in IMAGE_PATH_RE.finditer(text):
        if len(parts) >= MAX_IMAGES_PER_MESSAGE:
            break
        raw = match.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        part = image_path_to_part(expand_path(raw, cwd))
        if part is not None:
            parts.append(part)
    return parts


# ---------------------------------------------------------------- shell

_SHELL_CACHE: dict[str, str] = {}
_SHELL_REASON: dict[str, str] = {}


def _shell_candidates() -> list[tuple[str, str]]:
    """(label, executable) candidates in preference order for this OS."""
    candidates: list[tuple[str, str]] = []
    if os.name == "nt":
        override = os.environ.get("OASET_SHELL_PATH")
        if override:
            candidates.append(("OASET_SHELL_PATH", override))
        for name in ("bash", "pwsh", "powershell"):
            found = shutil.which(name)
            if found:
                candidates.append((name, found))
        candidates.append(("cmd", os.environ.get("COMSPEC") or "cmd.exe"))
    else:
        for name in (os.environ.get("SHELL") or "", "bash", "sh"):
            found = shutil.which(name) if name else ""
            if found:
                candidates.append((name, found))
        candidates.append(("sh", "/bin/sh"))
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for label, path in candidates:
        if path and path.lower() not in seen:
            seen.add(path.lower())
            unique.append((label, path))
    return unique


def _shell_probe(path: str) -> tuple[bool, str]:
    """Does this shell actually run a command? Returns (usable, reason)."""
    name = os.path.basename(path).lower().removesuffix(".exe")
    if name in ("bash", "sh", "zsh", "dash", "ksh"):
        argv = [path, "-c", "echo oaset-shell-ok"]
    elif name in ("powershell", "pwsh"):
        argv = [path, "-NoProfile", "-Command", "echo oaset-shell-ok"]
    else:
        argv = [path, "/c", "echo oaset-shell-ok"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired:
        return False, "timed out after 8s"
    except OSError as exc:
        return False, f"cannot start: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"{type(exc).__name__}: {exc}"
    combined = f"{proc.stdout or ''}{proc.stderr or ''}"
    if proc.returncode == 0 and "oaset-shell-ok" in combined:
        return True, ""
    detail = " ".join(combined.split())[:160]
    return False, f"exit {proc.returncode}: {detail}" if detail else f"exit {proc.returncode}"


def detect_shell(refresh: bool = False) -> str:
    """Best-effort *usable* shell binary for run_shell.

    `shutil.which("bash")` on Windows finds the WSL launcher at
    C:\\Windows\\system32\\bash.exe, which is present even when WSL has no
    distribution installed — on such a machine every single run_shell call
    failed with "execvpe(/bin/bash) failed", and the system prompt told the
    model it had bash. Candidates are therefore *probed*, and the first one that
    actually runs a command wins.
    """
    key = f"{os.name}:{os.environ.get('OASET_SHELL_PATH', '')}"
    if not refresh and key in _SHELL_CACHE:
        return _SHELL_CACHE[key]
    reasons: list[str] = []
    for label, path in _shell_candidates():
        usable, reason = _shell_probe(path)
        if usable:
            _SHELL_CACHE[key] = path
            _SHELL_REASON[key] = "" if label != "OASET_SHELL_PATH" else "from OASET_SHELL_PATH"
            return path
        reasons.append(f"{label}({path}): {reason}")
    fallback = "cmd" if os.name == "nt" else "/bin/sh"
    _SHELL_CACHE[key] = fallback
    _SHELL_REASON[key] = "no candidate shell worked — " + "; ".join(reasons)
    return fallback


def shell_diagnosis(refresh: bool = False) -> str:
    """Why the detected shell is what it is ("" when it probed clean)."""
    detect_shell(refresh=refresh)
    key = f"{os.name}:{os.environ.get('OASET_SHELL_PATH', '')}"
    return _SHELL_REASON.get(key, "")


def shell_command_line(command: str) -> list[str]:
    """argv for running `command` in the detected shell, with UTF-8 output pinned.

    Windows PowerShell 5.1 writes redirected stdout in UTF-16LE by default, so
    a hook that appends to a log file produced a BOM that later broke every
    UTF-8 reader (`UnicodeDecodeError: byte 0xff`). Pinning the output encoding
    inside the command fixes it for hooks, run_shell and background tasks alike.
    """
    shell = detect_shell()
    name = os.path.basename(shell).lower().removesuffix(".exe")
    if name in ("bash", "sh", "zsh", "dash", "ksh"):
        return [shell, "-c", command]
    if name == "cmd":
        return [shell, "/c", command]
    if name in ("powershell", "pwsh"):
        return [shell, "-NoProfile", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
    return [shell, "-c", command]

