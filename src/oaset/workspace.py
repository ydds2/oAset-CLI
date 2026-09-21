"""Workspace — G1: the desktop-shell substrate.

One workspace, N named sessions, N SessionHosts, ONE audit trail. The TUI
uses a single implicit host today; a desktop shell opens many windows/tabs
over the same workspace. The audit log (JSONL, one line per tool call) is
the security story a desktop surface needs: what ran, with what arguments,
how long, in which session — written locally, never shipped anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

from oaset.config import AppConfig, load_config
from oaset.utils import oaset_home


class Workspace:
    def __init__(self, cfg: AppConfig | None = None, cwd: Path | None = None, *,
                 audit: bool = True, home: Path | None = None) -> None:
        self.cfg = cfg or load_config()
        self.cwd = Path(cwd or Path.cwd())
        self.home = home or oaset_home()
        self.audit_enabled = audit
        self.hosts: dict[str, Any] = {}
        self._labels: dict[int, str] = {}
        self._audit_lock = asyncio.Lock()
        self.audit_path = self.home / "audit" / (time.strftime("%Y%m%d") + ".jsonl")

    # ------------------------------------------------------------- hosts

    def host(self, name: str, **kwargs: Any):
        """Create (or return) the named session host inside this workspace."""
        if name in self.hosts:
            return self.hosts[name]
        from oaset.host import SessionHost

        session_host = SessionHost(self.cfg, kwargs.pop("cwd", self.cwd), **kwargs)
        self.hosts[name] = session_host
        if self.audit_enabled:
            def _audit(tool: str, arguments: str, is_error: bool, elapsed_ms: float,
                       *, _host=session_host, _label=name) -> None:
                record = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "session": _host.session.meta.session_id,
                    "host": _label,
                    "tool": tool,
                    "args": (arguments or "")[:2000],
                    "error": bool(is_error),
                    "ms": round(float(elapsed_ms), 1),
                }
                asyncio.ensure_future(self._audit_write(record))
            session_host.registry.ctx.audit = _audit
        return session_host

    # ------------------------------------------------------------- audit

    async def _audit_write(self, record: dict) -> None:
        async with self._audit_lock:
            await asyncio.to_thread(self._append, record)

    def _append(self, record: dict) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_audit(self) -> list[dict]:
        """The audit trail for inspection/tests (newest last)."""
        if not self.audit_path.is_file():
            return []
        return [json.loads(line) for line in
                self.audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # ------------------------------------------------------------- close

    async def aclose(self) -> None:
        for host in list(self.hosts.values()):
            with contextlib.suppress(Exception):
                await host.aclose()
        self.hosts.clear()
