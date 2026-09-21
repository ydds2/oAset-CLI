"""Recently used slash commands, persisted for the palette's "recent" group.

Kept deliberately small and failure-tolerant: this is UI convenience, so a
missing/corrupt/read-only state file must never break the TUI - it just means
no recent group this session.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

_MAX_ENTRIES = 40


def _path() -> Path:
    from oaset.utils import oaset_home

    return oaset_home() / "command_usage.json"


def _load() -> dict[str, dict]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def record(name: str) -> None:
    """Count one dispatch of `name` (the canonical command name, not an alias)."""
    if not name:
        return
    data = _load()
    entry = data.get(name) or {}
    entry["count"] = int(entry.get("count") or 0) + 1
    entry["last"] = time.time()
    data[name] = entry
    if len(data) > _MAX_ENTRIES:
        # keep the most recently used entries only
        ordered = sorted(data.items(), key=lambda kv: kv[1].get("last") or 0,
                         reverse=True)[:_MAX_ENTRIES]
        data = dict(ordered)
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass  # convenience only: never break a command because of this


def recent(limit: int = 5) -> list[str]:
    """Most recently used command names (newest first), capped at `limit`."""
    data = _load()
    ordered = sorted(data.items(), key=lambda kv: kv[1].get("last") or 0, reverse=True)
    return [name for name, _ in ordered[: max(0, limit)]]
