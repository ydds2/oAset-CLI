"""Per-provider API key storage: ~/.oaset/credentials/<provider>.json (0600 best-effort)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from oaset.utils import now_iso, oaset_home


def credentials_dir(home: Path | None = None) -> Path:
    base = (home or oaset_home()) / "credentials"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _credential_path(provider: str, home: Path | None = None) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in provider)
    return credentials_dir(home) / f"{safe}.json"


def save_credential(provider: str, api_key: str, home: Path | None = None) -> None:
    """Atomic + cross-process locked: a half-written key file would brick the
    provider on every later start, so the write is tmp+replace under a lock."""
    from oaset.utils import atomic_write_text, file_lock

    path = _credential_path(provider, home)
    body = json.dumps({"provider": provider, "api_key": api_key, "updated_at": now_iso()},
                      indent=2)
    with file_lock(path):
        atomic_write_text(path, body)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows FAT/ACL filesystems: best-effort only


def load_credential(provider: str, home: Path | None = None) -> str:
    path = _credential_path(provider, home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("api_key", ""))
    except (OSError, json.JSONDecodeError, ValueError):
        return ""


def delete_credential(provider: str, home: Path | None = None) -> bool:
    path = _credential_path(provider, home)
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def list_credentials(home: Path | None = None) -> list[str]:
    try:
        return sorted(p.stem for p in credentials_dir(home).glob("*.json"))
    except OSError:
        return []
