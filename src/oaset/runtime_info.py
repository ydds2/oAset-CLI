"""Where is the running code from? - the answer to "am I on an old build?".

Three things can answer to `oaset`, and only one of them is a release:

- a **packaged build** (PyInstaller) that carries a stamp written by CI;
- an **editable install / checkout**, which tracks the working tree and is a
  development runtime, not a product build;
- a packaged build whose stamp predates the code beside it - the stale artifact
  that used to be indistinguishable in the UI.

So every entry point (/status, /version, ``oaset --version``) states which one it
is, and a packaged build also prints its fingerprint (commit + content digest).
Two builds of the same version therefore never look identical, and a stale one
is visible at a glance.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path


def _git_commit(start: Path) -> tuple[str, str]:
    """(short-sha, branch) from the working tree, without spawning git."""
    directory = start
    for _ in range(6):  # walk up a few levels looking for .git
        git = directory / ".git"
        if git.is_dir():
            head = git / "HEAD"
            break
        if git.is_file():
            # worktree/submodule: ".git" is a file pointing at the real dir
            try:
                pointer = git.read_text(encoding="utf-8").strip()
                if pointer.startswith("gitdir:"):
                    head = Path(pointer.split(":", 1)[1].strip()) / "HEAD"
                    break
            except Exception:
                return "", ""
        directory = directory.parent
    else:
        return "", ""

    try:
        content = head.read_text(encoding="utf-8").strip()
    except Exception:
        return "", ""
    if content.startswith("ref:"):
        ref = content.split(":", 1)[1].strip()
        branch = ref.rsplit("/", 1)[-1]
        try:
            sha = (directory / ".git" / ref).read_text(encoding="utf-8").strip()
        except Exception:
            sha = ""
        return (sha[:7], branch) if sha else ("", branch)
    return (content[:7], "")  # detached HEAD: the file holds the sha


def build_stamp() -> dict[str, str] | None:
    """The CI-written stamp, when this is a packaged build."""
    try:
        from oaset import _build_info  # type: ignore[attr-defined]
    except Exception:
        return None
    return {
        "version": getattr(_build_info, "VERSION", ""),
        "commit": getattr(_build_info, "COMMIT", ""),
        "branch": getattr(_build_info, "BRANCH", ""),
        "built_at": getattr(_build_info, "BUILT_AT", ""),
        "fingerprint": getattr(_build_info, "FINGERPRINT", ""),
        "content_sha256": getattr(_build_info, "CONTENT_SHA256", ""),
    }


def running_source() -> dict[str, str]:
    """kind: 'release' (stamped, frozen package) | 'dev' (checkout) | 'unknown'.

    A stamp alone does not make a release: a developer who ran stamp_build.py in
    their checkout is still running raw files, so only a frozen process reports
    'release' - otherwise a dev tree could masquerade as a published build.
    """
    frozen = bool(getattr(sys, "frozen", False))
    stamp = build_stamp()
    if frozen:
        exe = Path(sys.executable)
        try:
            built = time.strftime("%Y-%m-%d %H:%M", time.localtime(exe.stat().st_mtime))
        except Exception:
            built = ""
        return {
            "kind": "release",
            "path": str(exe),
            "build": (stamp or {}).get("built_at") or built,
            "commit": (stamp or {}).get("commit", ""),
            "branch": (stamp or {}).get("branch", ""),
            "fingerprint": (stamp or {}).get("fingerprint", "") or "UNSTAMPED",
        }

    try:
        import oaset

        package = Path(oaset.__file__).resolve().parent
    except Exception:  # pragma: no cover - defensive
        return {"kind": "unknown", "path": "", "build": "", "commit": "",
                "branch": "", "fingerprint": ""}
    sha, branch = _git_commit(package)
    return {"kind": "dev", "path": str(package), "build": "", "commit": sha,
            "branch": branch, "fingerprint": ""}


def describe_source() -> str:
    """One human-readable line (used by /status, /version and oaset --version)."""
    info = running_source()
    if info["kind"] == "release":
        return (f"release {info['fingerprint']} · built {info['build'] or '?'} · "
                f"{info['path']}")
    if info["kind"] == "dev":
        where = info["commit"] or "(no git metadata)"
        branch = f"@{info['branch']}" if info["branch"] else ""
        return f"dev source{branch} · {where} · NOT a release build · {info['path']}"
    return "unknown source"
