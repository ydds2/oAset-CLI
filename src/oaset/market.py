"""Community plugin market.

Sources model mirrors DeepSeek Harness's market: you can add multiple
sources, but only ONE is active at a time — browsing and installing always go
through the active source. The built-in ``official`` source (the Update
Center catalog) is always present and cannot be removed.

Every install goes through ``UpdateManager.apply``: network policy is
enforced (local_only refuses), the package sha256 is verified against the
catalog entry, artifact signatures are honoured when present, and the write
is transactional with rollback. The market adds no second download path.

State lives in ``~/.oaset/market.json``:
    {"active": "official", "sources": [{"name": "...", "url": "https://..."}]}
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oaset.i18n import t
from oaset.update import (
    DEFAULT_CATALOG_URL,
    CatalogEntry,
    CatalogSource,
    UpdateError,
    UpdateManager,
    UpdateResult,
    _assert_source_allowed,
)
from oaset.utils import oaset_home

OFFICIAL_SOURCE = "official"


@dataclass
class MarketSource:
    name: str
    url: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "url": self.url}


def market_state_path(home: Path | None = None) -> Path:
    return (home or oaset_home()) / "market.json"


def official_source() -> MarketSource:
    return MarketSource(name=OFFICIAL_SOURCE, url=DEFAULT_CATALOG_URL)


def load_state(home: Path | None = None) -> dict[str, Any]:
    path = market_state_path(home)
    if not path.is_file():
        return {"active": OFFICIAL_SOURCE, "sources": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": OFFICIAL_SOURCE, "sources": []}
    if not isinstance(data, dict):
        return {"active": OFFICIAL_SOURCE, "sources": []}
    sources = [
        MarketSource(name=str(s.get("name", "")), url=str(s.get("url", "")))
        for s in (data.get("sources") or [])
        if isinstance(s, dict) and s.get("name") and s.get("url")
    ]
    active = str(data.get("active") or OFFICIAL_SOURCE)
    known = {OFFICIAL_SOURCE} | {s.name for s in sources}
    if active not in known:
        active = OFFICIAL_SOURCE
    return {"active": active, "sources": sources}


def save_state(home: Path | None, state: dict[str, Any]) -> None:
    path = market_state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "active": state.get("active", OFFICIAL_SOURCE),
        "sources": [s.as_dict() for s in state.get("sources", [])],
    }
    from oaset.utils import atomic_write_text, file_lock

    with file_lock(path):
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def read_plugin_manifest(path: Path) -> dict[str, Any]:
    """OASET_MANIFEST without executing plugin code: literal-eval the AST."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "OASET_MANIFEST":
                try:
                    data = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return {}
                return data if isinstance(data, dict) else {}
    return {}


class MarketManager:
    """Browses and installs plugins from the ACTIVE market source.

    Reuses the Update Center end to end: catalog fetch/cache, network policy,
    sha256 + artifact signature verification, transactional apply with
    rollback and history. The market only owns the source registry and the
    installed-plugin view.
    """

    def __init__(self, cfg: Any, home: Path | None = None, fetch: Any | None = None):
        self.cfg = cfg
        self.home = home or oaset_home()
        self.fetch = fetch

    # ------------------------------------------------------------- sources

    def sources(self) -> list[MarketSource]:
        state = load_state(self.home)
        return [official_source(), *state["sources"]]

    def active(self) -> MarketSource:
        state = load_state(self.home)
        for source in self.sources():
            if source.name == state["active"]:
                return source
        return official_source()

    def add_source(self, name: str, url: str) -> MarketSource:
        name = str(name or "").strip()
        url = str(url or "").strip()
        if not name or name == OFFICIAL_SOURCE or "::" in name:
            raise UpdateError("market_source_name", t("mkt_bad_name", name=name or "''"))
        state = load_state(self.home)
        if any(s.name == name for s in state["sources"]):
            raise UpdateError("market_source_name", t("mkt_duplicate", name=name))
        _assert_source_allowed(url)  # plain http(s) only — no file://, no UNC
        state["sources"].append(MarketSource(name=name, url=url))
        save_state(self.home, state)
        return MarketSource(name=name, url=url)

    def remove_source(self, name: str) -> bool:
        state = load_state(self.home)
        before = len(state["sources"])
        state["sources"] = [s for s in state["sources"] if s.name != name]
        if len(state["sources"]) == before:
            return False
        if state["active"] == name:
            state["active"] = OFFICIAL_SOURCE
        save_state(self.home, state)
        return True

    def use_source(self, name: str) -> MarketSource:
        source = next((s for s in self.sources() if s.name == name), None)
        if source is None:
            raise UpdateError("market_source_missing", t("mkt_unknown", name=name))
        state = load_state(self.home)
        state["active"] = name
        save_state(self.home, state)
        return source

    # ------------------------------------------------------------- browsing

    def _manager(self) -> UpdateManager:
        manager = UpdateManager(self.cfg, home=self.home, fetch=self.fetch)
        active = self.active()
        manager.source = CatalogSource(name=active.name, url=active.url)
        return manager

    async def discover(self, *, refresh: bool = False) -> tuple[list[CatalogEntry], list[str]]:
        """Plugin entries from the active source. Returns (entries, notes).

        A failed forced refresh falls back to the cached view; the error lands
        in notes (structured UpdateError codes, e.g. network_blocked under
        local_only) so callers can surface it instead of silently showing a
        stale market."""
        manager = self._manager()
        try:
            result = await (manager.refresh("plugin") if refresh else manager.check("plugin"))
        except UpdateError:
            result = await manager.check("plugin")  # cache + structured errors
        notes = [*result.notes, *result.errors]
        return list(result.entries), notes

    def installable(
        self, entries: list[CatalogEntry], installed: list[dict[str, Any]] | None = None
    ) -> list[CatalogEntry]:
        have = {i["name"] for i in (installed if installed is not None else self.installed())}
        return [e for e in entries if e.name not in have]

    # ------------------------------------------------------------ installed

    def _user_plugin_dir(self) -> Path:
        return self.home / "plugins"

    def installed(self) -> list[dict[str, Any]]:
        """Plugins the user manages (USER dir only — project plugins belong to
        the repo's .oaset/plugins and follow the trust-plugin flow instead)."""
        base = self._user_plugin_dir()
        if not base.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in sorted(base.glob("*.py")):
            manifest = read_plugin_manifest(path)
            out.append({
                "name": path.stem,
                "path": path,
                "version": str(manifest.get("version", "")),
                "description": str(manifest.get("description", "")),
                "permissions": tuple(str(p) for p in (manifest.get("permissions") or ())),
                "manifest": manifest,
            })
        return out

    # ------------------------------------------------------- install/remove

    async def install(self, name: str, dry_run: bool = False) -> tuple[UpdateResult, CatalogEntry]:
        entries, _ = await self.discover()
        entry = next((e for e in entries if e.name == name), None)
        if entry is None:
            raise UpdateError("market_missing_entry", t("mkt_not_in_source", name=name, source=self.active().name))
        result = await self._manager().apply([name], dry_run=dry_run)
        if result.errors:
            raise UpdateError("market_apply_failed", "; ".join(result.errors))
        return result, entry

    def uninstall(self, name: str) -> Path:
        path = self._user_plugin_dir() / f"{Path(name).name}.py"
        if not path.is_file():
            raise UpdateError("market_not_installed", t("mkt_not_installed", name=name))
        path.unlink()
        return path
