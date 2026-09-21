"""Update Center domain — explicit, user-triggered discovery of available updates.

Scope of this module (see git history: docs/ARCHITECTURE.zh-CN.md, deleted 2026-09-13):
  - CatalogSource/CatalogEntry/UpdatePlan/UpdateResult data model
  - UpdateManager with check / refresh / list / plan (read-only operations)
  - Local cached catalog under ~/.oaset/update/catalog.json with TTL
  - All network access (refresh) goes through NetworkPolicy.assert_allowed("pull")

apply / rollback / install run through the transactional history
(recorded backups per transaction; `rollback <target>` restores the newest
matching transaction).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from oaset import __version__
from oaset.i18n import t
from oaset.network import NetworkPolicy
from oaset.utils import oaset_home

KINDS = ("core", "plugin", "mcp", "provider", "model", "skill")
CACHE_TTL_SECONDS = 24 * 3600
DEFAULT_CATALOG_URL = "https://oaset-cli.github.io/catalog/index.json"
MAX_PACKAGE_BYTES = 10 * 1024 * 1024  # refuse absurd downloads before hashing
FETCH_TIMEOUT_SECONDS = 30.0
APPLY_LOCK_STALE_SECONDS = 900  # a crashed apply must not lock the update center forever
ALLOWED_SOURCE_SCHEMES = ("https", "http")


def _assert_source_allowed(url: str, *, allow_plain_http: bool = False) -> None:
    """Every catalog/package URL must be http(s) — and plain http only when
    the caller has proven the content is signature-protected.

    Refuses `file://`, UNC and other schemes before any I/O so a hostile or
    mistyped catalog cannot turn a download into a local file read. Plain
    http is the supply-chain hole: with the default empty trust store the
    Ed25519 layer protects nothing, and a MITM on an http mirror can
    replace the catalog AND its sha256 — then `apply plugin` writes
    attacker Python into a directory that loads with full permissions.
    """
    parsed = urlparse(str(url or ""))
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SOURCE_SCHEMES:
        raise UpdateError("unsafe_source",
                          t("upd_url_scheme", url=repr(url)))
    if scheme == "http" and not allow_plain_http:
        raise UpdateError(
            "unsafe_source",
            "plain http is not allowed for update sources (config says "
            f"{url!r}); use https or a signature-verified local mirror")
    if not parsed.netloc:
        raise UpdateError("unsafe_source", t("upd_url_host", url=repr(url)))


def _safe_segment(text: str) -> str:
    """catalog-supplied name/version land in a path: strip separators and ..
    (the plugin path already validated this way; core backup did not)."""
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(text))
    return cleaned.replace("..", "_") or "entry"


def _copy_file(src: Path, dst: Path) -> None:
    """Module-level so tests can inject mid-transaction failures."""
    shutil.copyfile(src, dst)


def _restore_file(dest: str | Path, backup: str | None, created: bool) -> None:
    """Undo one recorded mutation: restore the backup, or delete a created file."""
    path = Path(dest)
    if backup is not None and Path(backup).exists():
        path.write_bytes(Path(backup).read_bytes())
    elif created and path.exists():
        path.unlink()


def _write_bytes_locked(dest: Path, payload: bytes) -> None:
    """Stage file writes through one cross-process lock + unique tmp name.

    A fixed ``.tmp`` name meant the TUI and an ACP server applying updates at
    the same time wrote each other's temp file and one replaced a HALF-WRITTEN
    target; the lock orders them, the unique suffix isolates the scratch.
    """
    import uuid as _uuid

    from oaset.utils import file_lock

    with file_lock(dest):
        tmp = dest.with_suffix(f".tmp-{_uuid.uuid4().hex[:8]}")
        try:
            tmp.write_bytes(payload)
            tmp.replace(dest)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise


def _write_json_atomic(path: Path, data: Any) -> None:
    from oaset.utils import atomic_write_text, file_lock

    with file_lock(path):
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _undo_json_key_edits(path: Path, edits: list[dict[str, Any]]) -> list[str]:
    """Undo [container, key] edits inside a JSON file (mcp.json)."""
    if not path.exists():
        return [t("upd_rollback_file_gone", path=path)]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [t("upd_rollback_read_failed", path=path, exc=exc)]
    if not isinstance(data, dict):
        return [t("upd_rollback_not_object", path=path)]
    try:
        for edit in edits:
            keys = [str(k) for k in (edit.get("path") or [])]
            if not keys:
                continue
            node = data
            for key in keys[:-1]:
                child = node.get(key)
                if not isinstance(child, dict):
                    child = {}
                    node[key] = child
                node = child
            if edit.get("previous") is None:
                node.pop(keys[-1], None)
            else:
                node[keys[-1]] = edit["previous"]
        _write_json_atomic(path, data)
    except OSError as exc:
        return [t("upd_rollback_write_failed", path=path, exc=exc)]
    return []


def _undo_toml_key_edits(path: Path, edits: list[dict[str, Any]]) -> list[str]:
    """Undo provider/model edits inside config.toml, key by key."""
    import oaset.config as cfgmod

    try:
        cfg = cfgmod.load_config()
        for edit in edits:
            keys = [str(k) for k in (edit.get("path") or [])]
            if len(keys) < 2:
                continue
            container_name, name = keys[0], keys[1]
            container: dict[str, Any]
            container = cast("dict[str, Any]", cfg.providers if container_name == "providers" else cfg.models)
            previous = edit.get("previous")
            if previous is None:
                container.pop(name, None)
            else:
                cls = cfgmod.ProviderConfig if container_name == "providers" else cfgmod.ModelConfig
                container[name] = cls(**previous)
        cfgmod.save_config(cfg)
    except Exception as exc:  # noqa: BLE001 - surfaced as a rollback diagnostic
        return [t("upd_rollback_key_failed", path=path, kind=type(exc).__name__, exc=exc)]
    return []


class FetchSession:
    """Owns the HTTP client for exactly one update operation.

    Regression this replaces: each call site used to do::

        async with httpx.AsyncClient() as client:
            self._fetch = client.get
        response = await self._fetch(url)

    The bound method was stored and then awaited *after* the ``async with``
    block exited, so every request ran against an already-closed client (and
    the stale callable leaked into later operations). A session now either owns
    an ``httpx.AsyncClient`` for the duration of its ``async with`` block, or
    wraps an injected fetch callable (tests / advanced users) which it never
    closes and never replaces.
    """

    def __init__(self, fetch: Any | None = None, *, timeout: float = FETCH_TIMEOUT_SECONDS) -> None:
        self._injected = fetch
        self._client: Any | None = None
        self.timeout = timeout

    @property
    def owns_client(self) -> bool:
        """True when this session opens (and therefore must close) a client."""
        return self._injected is None

    async def __aenter__(self) -> FetchSession:
        if self._injected is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
        return False

    @property
    def closed(self) -> bool:
        return self._injected is None and self._client is None

    async def get(self, url: str) -> Any:
        _assert_source_allowed(url)
        if self._injected is not None:
            return await self._injected(url)
        if self._client is None:
            # Never silently use a disposed client again.
            raise UpdateError("fetch_closed", t("upd_session_closed"))
        response = await self._client.get(url)
        # redirects are followed: an https source may have landed on plain
        # http mid-chain — the per-source gate must survive the whole hop
        final = str(getattr(response, "url", url) or url)
        if final != url:
            _assert_source_allowed(final)
        return response


class _Txn:
    """One apply call's staging area: unique directory, ordered steps, manifest.

    Every committed mutation records {seq, dest, backup, created}. A failure
    mid-transaction restores the already-committed steps in REVERSE order, so a
    multi-target apply is all-or-nothing at the file level instead of leaving
    half of the targets on the new version. Backups live in a unique per-run
    directory, never in a shared `<dest>.bak` slot that a second target could
    overwrite.
    """

    def __init__(self, home: Path, label: str = "apply", restore_tree: Any | None = None) -> None:
        self.id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"
        self.dir = home / "update" / "txn" / self.id
        self.label = label
        self.steps: list[dict[str, Any]] = []
        # directory-level restorer for core steps; injected to avoid a circular
        # dependency between _Txn and UpdateManager
        self.restore_tree = restore_tree

    @classmethod
    def from_manifest(cls, home: Path, manifest: dict[str, Any],
                      restore_tree: Any | None = None) -> _Txn:
        txn = cls(home, str(manifest.get("label") or "apply"), restore_tree)
        txn.id = str(manifest.get("txn") or txn.id)
        txn.dir = home / "update" / "txn" / txn.id
        txn.steps = [dict(s) for s in (manifest.get("steps") or [])]
        return txn

    def stage(self, dest: Path) -> dict[str, Any]:
        """Back `dest` up into the transaction dir and register the step."""
        self.dir.mkdir(parents=True, exist_ok=True)
        seq = len(self.steps) + 1
        if dest.exists():
            backup = self.dir / f"{seq:03d}-{dest.name}.bak"
            backup.write_bytes(dest.read_bytes())
            return self.record(dest, str(backup), created=False)
        return self.record(dest, None, created=True)

    def record(self, dest: Path, backup: str | None, *, created: bool,
               tree: bool = False, key_edits: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Register a step whose backup already exists elsewhere (core tree).

        `key_edits` lets a step be undone surgically instead of restoring the
        whole file, so rolling back one target of a shared config.toml /
        mcp.json does not silently revert sibling targets or unrelated edits
        the user made after the apply.
        """
        step = {"seq": len(self.steps) + 1, "dest": str(dest),
                "backup": backup, "created": bool(created), "tree": bool(tree)}
        if key_edits:
            step["key_edits"] = key_edits
        self.steps.append(step)
        return step

    def write_manifest(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "txn": self.id,
            "label": self.label,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "steps": self.steps,
        }
        tmp = self.dir / "manifest.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.dir / "manifest.json")

    def restore(self) -> list[str]:
        """Reverse-order restore of every committed step; returns diagnostics."""
        problems: list[str] = []
        for step in reversed(self.steps):
            try:
                if step.get("tree"):
                    backup = step.get("backup")
                    if self.restore_tree is None:
                        problems.append(t("upd_no_dir_restorer", dest=step['dest']))
                    elif not backup or not Path(backup).is_dir():
                        problems.append(t("upd_backup_missing", dest=step['dest']))
                    else:
                        self.restore_tree(Path(step["dest"]), Path(backup))
                else:
                    _restore_file(step["dest"], step["backup"], step["created"])
            except OSError as exc:
                problems.append(
                    t("upd_restore_failed", dest=step['dest'], kind=type(exc).__name__, exc=exc))
        return problems


class UpdateError(Exception):
    """Structured update failure (network-blocked, bad catalog, incompatible…)."""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


@dataclass(frozen=True)
class CatalogEntry:
    kind: str  # core | plugin | mcp | provider | model | skill
    name: str
    version: str
    description: str = ""
    requires_oaset: str = ""  # semver-ish constraint, "" = any
    source_url: str = ""
    sha256: str = ""
    published_at: str = ""
    permissions: tuple[str, ...] = ()
    signature: dict[str, Any] = field(default_factory=dict)  # artifact sig block
    install: dict[str, Any] = field(default_factory=dict)  # kind-specific payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CatalogEntry:
        kind = str(data.get("kind", "")).strip().lower()
        name = str(data.get("name", "")).strip()
        version = str(data.get("version", "")).strip()
        if kind not in KINDS:
            raise UpdateError("catalog_schema", f"catalog entry has invalid kind: {kind!r}")
        if not name or not version:
            raise UpdateError("catalog_schema", f"catalog entry missing name/version: {data!r}")
        install = data.get("install") or {}
        if not isinstance(install, dict):
            raise UpdateError("catalog_schema", f"catalog 'install' must be an object: {name!r}")
        signature = data.get("signature") or {}
        if not isinstance(signature, dict):
            raise UpdateError("catalog_schema", f"catalog 'signature' must be an object: {name!r}")
        return cls(
            kind=kind,
            name=name,
            version=version,
            description=str(data.get("description", "")),
            requires_oaset=str(data.get("requires_oaset", "")),
            source_url=str(data.get("source_url", "")),
            sha256=str(data.get("sha256", "")),
            published_at=str(data.get("published_at", "")),
            permissions=tuple(str(p) for p in (data.get("permissions") or ())),
            signature=dict(signature),
            install=dict(install),
        )


@dataclass(frozen=True)
class CatalogSource:
    name: str
    url: str
    trusted: bool = False

    @classmethod
    def default(cls) -> CatalogSource:
        # The bundled catalog is read-only metadata; it is never auto-installed.
        return cls(name="official", url=DEFAULT_CATALOG_URL, trusted=True)


@dataclass
class UpdatePlan:
    targets: list[CatalogEntry] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (entry.name, reason)

    def render(self) -> str:
        lines = [t("upd_plan_title")]
        if not self.targets:
            lines.append(t("upd_plan_empty"))
        for e in self.targets:
            lines.append(t("upd_plan_entry", kind=e.kind, name=e.name, version=e.version, source=e.source_url or t("upd_source_local")))
        for name, reason in self.skipped:
            lines.append(f"✗ {name}: {reason}")
        lines.append(t("upd_plan_hint"))
        return "\n".join(lines)


@dataclass
class UpdateResult:
    action: str
    entries: list[CatalogEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    refreshed_at: float = 0.0
    from_cache: bool = False

    def render(self, detail: bool = False) -> str:
        header = {
            "check": t("upd_check_title"),
            "refresh": t("upd_refresh_title"),
            "list": t("upd_list_title"),
            "plan": t("upd_plan_title"),
        }.get(self.action, t("upd_plan_action_title", action=self.action))
        lines = [header]
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.refreshed_at)) if self.refreshed_at else "(never)"
        lines.append(t("upd_result_when", when=when, cached=t("upd_from_cache") if self.from_cache else "", version=__version__))
        if not self.entries:
            lines.append(t("upd_entries_empty"))
        for e in self.entries:
            marker = "●" if self._is_newer(e) else " "
            lines.append(
                f"{marker} {e.kind:<8} {e.name} @ {e.version}"
                + (f"  — {e.description}" if e.description else "")
            )
            if detail:
                src = e.source_url or "(local)"
                digest = e.sha256[:16] + "…" if e.sha256 else "(none)"
                lines.append(t("upd_detail_source", src=src, digest=digest))
                if e.requires_oaset:
                    lines.append(t("upd_detail_compat", req=e.requires_oaset))
                if e.permissions:
                    lines.append(t("upd_detail_perms", perms=', '.join(e.permissions)))
                if e.published_at:
                    lines.append(t("upd_detail_published", published=e.published_at))
        for err in self.errors:
            lines.append(f"✗ {err}")
        for note in self.notes:
            lines.append(note)
        return "\n".join(lines)

    def _is_newer(self, entry: CatalogEntry) -> bool:
        if entry.kind != "core":
            return True
        return _version_cmp(entry.version, __version__) > 0


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.strip().split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _version_cmp(a: str, b: str) -> int:
    """Numeric dotted-version compare; longer tuple pads with zeros (1.2 == 1.2.0)."""
    ta, tb = list(_version_tuple(a)), list(_version_tuple(b))
    width = max(len(ta), len(tb))
    ta += [0] * (width - len(ta))
    tb += [0] * (width - len(tb))
    return (ta > tb) - (ta < tb)


def _catalog_cache_path(home: Path | None = None) -> Path:
    base = home or oaset_home()
    return base / "update" / "catalog.json"


def _parse_catalog(payload: str) -> list[CatalogEntry]:
    data = json.loads(payload)
    if not isinstance(data, dict) or "entries" not in data:
        raise UpdateError("catalog_schema", "catalog root must be an object with 'entries'")
    raw_entries = data["entries"]
    if not isinstance(raw_entries, list):
        raise UpdateError(
            "catalog_schema",
            f"catalog 'entries' must be a list, got {type(raw_entries).__name__}",
        )
    return [CatalogEntry.from_dict(item) for item in raw_entries]


def _version_at_least(current: str, constraint: str) -> bool:
    """Constraint check: '>=X.Y.Z' or ''. Uses the same numeric comparison as
    _version_cmp so prerelease tags ('1.2.0-alpha') compare consistently
    instead of raising on int()."""
    constraint = constraint.strip()
    if not constraint:
        return True
    if constraint.startswith(">="):
        return _version_cmp(current, constraint[2:].strip()) >= 0
    raise UpdateError("catalog_schema", f"unsupported constraint: {constraint!r}")


class UpdateManager:
    """Read-only Update Center core shared by the TUI (/update) and CLI (oaset update)."""

    def __init__(self, cfg: Any, home: Path | None = None, fetch: Any | None = None,
                 package_dir: Path | None = None,
                 trusted_keys: dict[str, str] | None = None):
        self.cfg = cfg
        self.policy = NetworkPolicy.from_config(cfg)
        self.home = home or oaset_home()
        # Mirror-friendly: [update] catalog_url overrides the official catalog
        # (GitHub Pages is unreliable in mainland China). Validated with the
        # same http(s)-only gate as every other catalog/package URL.
        override = str(getattr(cfg, "update_catalog_url", "") or "").strip()
        if override:
            _assert_source_allowed(override)
            self.source = CatalogSource(name="mirror", url=override, trusted=True)
        else:
            self.source = CatalogSource.default()
        # injectable httpx-like fetch for tests: async get(url) -> response with .text
        self._fetch = fetch
        # injectable live package dir for core transactions (tests); None =
        # auto-detect with the source-checkout guard
        self.package_dir = Path(package_dir) if package_dir is not None else None
        # SUPPLY-01: publisher public keys, base64-encoded, by key_id. Catalog
        # and artifact signatures verify against exactly these keys.
        self.trusted_keys: dict[str, str] = dict(
            trusted_keys if trusted_keys is not None
            else getattr(cfg, "update_trusted_keys", {}) or {})

    # ------------------------------------------------------------------ cache

    def _read_cache(self) -> tuple[list[CatalogEntry], float]:
        path = _catalog_cache_path(self.home)
        if not path.exists():
            return [], 0.0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = [CatalogEntry.from_dict(item) for item in data.get("entries", [])]
            return entries, float(data.get("refreshed_at", 0.0))
        except (OSError, ValueError, UpdateError):
            return [], 0.0

    def _write_cache(self, entries: list[CatalogEntry], refreshed_at: float) -> None:
        path = _catalog_cache_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": self.source.name,
            "refreshed_at": refreshed_at,
            "entries": [
                {
                    "kind": e.kind, "name": e.name, "version": e.version,
                    "description": e.description, "requires_oaset": e.requires_oaset,
                    "source_url": e.source_url, "sha256": e.sha256,
                    "published_at": e.published_at, "permissions": list(e.permissions),
                    "signature": e.signature,
                    "install": e.install,
                }
                for e in entries
            ],
        }
        try:
            from oaset.utils import atomic_write_text, file_lock

            with file_lock(path):
                atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError as exc:
            raise UpdateError("cache_write_failed", t("upd_cache_write_failed", exc=exc)) from exc

    # ---------------------------------------------------------------- actions

    async def run(self, action: str, kind: str | None = None) -> "UpdateResult | UpdatePlan":
        if action == "check":
            return await self.check(kind)
        if action == "refresh":
            return await self.refresh(kind)
        if action == "list":
            return self.list_entries(kind)
        if action == "plan":
            return self.plan(kind)
        raise UpdateError("unsupported_action", f"update action not supported: {action!r}",
                          t("upd_supported_actions"))

    async def check(self, kind: str | None = None) -> UpdateResult:
        entries, refreshed_at = self._read_cache()
        if kind and kind != "all":
            entries = [e for e in entries if e.kind == kind]
        from_cache = refreshed_at > 0
        if refreshed_at + CACHE_TTL_SECONDS < time.time():
            try:
                fresh = await self.refresh(kind)
                return fresh
            except UpdateError as exc:
                result = UpdateResult(action="check", entries=entries, refreshed_at=refreshed_at,
                                      from_cache=from_cache)
                result.errors.append(f"{exc.code}: {exc}" + (f"（{exc.hint}）" if exc.hint else ""))
                return result
        return UpdateResult(action="check", entries=entries, refreshed_at=refreshed_at,
                            from_cache=from_cache)

    async def refresh(self, kind: str | None = None) -> UpdateResult:
        from oaset.network import NetworkBlockedError

        try:
            self.policy.assert_allowed("pull")  # local_only must refuse, structured
        except NetworkBlockedError as exc:
            raise UpdateError("network_blocked", str(exc), getattr(exc, "hint", "")) from exc
        # The session owns the client for exactly this request; self._fetch is
        # never replaced by a bound method of a client that is about to close.
        async with FetchSession(self._fetch) as session:
            try:
                response = await session.get(self.source.url)
                text = getattr(response, "text", "")
                data = json.loads(text)
                if not isinstance(data, dict) or "entries" not in data:
                    raise UpdateError("catalog_schema",
                                      "catalog root must be an object with 'entries'")
                entries = _parse_catalog(text)
            except UpdateError:
                raise
            except Exception as exc:  # httpx.HTTPError and friends → structured failure
                raise UpdateError("fetch_failed",
                                  t("upd_fetch_failed", kind=type(exc).__name__, exc=exc)) from exc
            # SUPPLY-01: a catalog that CLAIMS a publisher signature must
            # verify against the trust store before it can touch the cache —
            # a tampered catalog is rejected whole, the old cache stays usable.
            from oaset.supply import catalog_signature_block, verify_catalog_signature

            signed_by: str | None = None
            if catalog_signature_block(data) is not None:
                try:
                    signed_by = verify_catalog_signature(data, self.trusted_keys)
                except ValueError as exc:
                    raise UpdateError(
                        "catalog_signature_invalid",
                        t("upd_cat_sig_failed", exc=exc),
                        t("upd_cat_sig_hint")) from exc
        now = time.time()
        self._write_cache(entries, now)  # cache stays full-catalog; filter is result-level
        if kind and kind != "all":
            entries = [e for e in entries if e.kind == kind]
        result = UpdateResult(action="refresh", entries=entries, refreshed_at=now,
                              from_cache=False)
        if signed_by:
            result.notes.append(t("upd_cat_signed", key_id=signed_by))
        else:
            result.notes.append(t("upd_cat_unsigned"))
        return result

    def _verify_artifact_signature(self, entry: CatalogEntry, payload: bytes) -> None:
        """SUPPLY-01: per-entry artifact signature over the exact downloaded
        bytes. An entry WITHOUT a signature block passes (checksum-only
        catalogs stay installable); a PRESENT block that fails verification
        aborts the apply — a mirror cannot swap in different bytes."""
        from oaset.supply import verify_artifact_signature

        if not entry.signature:
            return
        try:
            verify_artifact_signature(entry, payload, self.trusted_keys)
        except ValueError as exc:
            raise UpdateError(
                "artifact_signature_invalid",
                t("upd_art_sig_failed", name=entry.name, exc=exc),
                t("upd_art_sig_hint"))

    async def _download(self, url: str, limit: int, label: str) -> bytes:
        """Fetch one package payload inside a live session, with a size cap.

        The payload is materialised *inside* the session block so no response
        object outlives the client that produced it.
        """
        async with FetchSession(self._fetch) as session:
            try:
                response = await session.get(url)
                payload = getattr(response, "content", None)
                if payload is None:
                    body = getattr(response, "text", "")
                    payload = body if isinstance(body, bytes) else str(body).encode("utf-8")
            except UpdateError:
                raise
            except Exception as exc:
                raise UpdateError("fetch_failed",
                                  t("upd_download_failed", label=label, kind=type(exc).__name__, exc=exc)) from exc
        if payload is None:
            raise UpdateError("verification_failed", t("upd_download_empty", label=label))
        if len(payload) > limit:
            raise UpdateError("verification_failed",
                              t("upd_too_large", label=label, size=len(payload), limit=limit))
        return payload

    def list_entries(self, kind: str | None = None) -> UpdateResult:
        entries, refreshed_at = self._read_cache()
        if kind and kind != "all":
            entries = [e for e in entries if e.kind == kind]
        return UpdateResult(action="list", entries=entries, refreshed_at=refreshed_at, from_cache=True)

    def plan(self, kind: str | None = None) -> UpdatePlan:
        entries, _ = self._read_cache()
        if kind and kind != "all":
            entries = [e for e in entries if e.kind == kind]
        plan = UpdatePlan()
        for entry in entries:
            if entry.kind == "core" and _version_cmp(entry.version, __version__) <= 0:
                plan.skipped.append((entry.name, t("upd_already_current", current=__version__, version=entry.version)))
                continue
            if not _version_at_least(__version__, entry.requires_oaset):
                plan.skipped.append((entry.name, f"requires oaset {entry.requires_oaset}"))
                continue
            plan.targets.append(entry)
        return plan

    # ------------------------------------------------- history / apply / rollback

    def _history_path(self) -> Path:
        return self.home / "update" / "history.jsonl"

    def _append_history(self, record: dict[str, Any]) -> None:
        path = self._history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        path = self._history_path()
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
        return out

    def _plugin_dest(self, name: str) -> Path:
        """Plugin installs go to the global plugins dir as <name>.py only.

        Rejects anything that is not a bare filename: path traversal in a
        catalog must never write outside the plugins directory.
        """
        if not name or Path(name).name != name or "/" in name or "\\" in name or ".." in name:
            raise UpdateError("unsafe_target", f"unsafe plugin target name: {name!r}")
        return self.home / "plugins" / f"{name}.py"

    @contextlib.contextmanager
    def _apply_lock(self):
        """Single-writer guard so two applies cannot interleave backups/writes."""
        lock = self.home / "update" / "apply.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        if lock.exists():
            with contextlib.suppress(OSError, ValueError):
                age = time.time() - float(lock.read_text(encoding="utf-8").strip() or 0)
                if age > APPLY_LOCK_STALE_SECONDS:
                    lock.unlink(missing_ok=True)  # crashed run must not lock forever
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise UpdateError(
                "apply_busy",
                t("upd_busy"),
                t("upd_busy_hint", lock=lock)) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(time.time()))
            yield lock
        finally:
            with contextlib.suppress(OSError):
                lock.unlink()

    async def apply(self, targets: list[str] | None = None, dry_run: bool = False) -> UpdateResult:
        """Install entries from the cached catalog, dispatched by kind.

        - plugin: download → sha256 verify → backup → atomic replace
        - mcp: merge server spec into ~/.oaset/mcp.json (backup + atomic write)
        - provider/model: merge entries into config.toml (adds ONLY; never
          switches default_model — enabling a model requires explicit user
          verification per the plan)
        - core: transactional bundle upgrade — download zip → sha256 verify →
          zip-slip guard → stage → backup the package dir → file-level
          replace with automatic restore on failure; source/editable
          checkouts are refused (use pip for those).

        One call is ONE transaction: every mutation is staged into a unique
        directory, a manifest records the steps, and a failure restores the
        committed steps in reverse order before the error is returned. Every
        mutation records {dest, backup|None, created} in history so rollback
        restores exactly the previous state.

        ``dry_run`` downloads and verifies only: it must not mutate the live
        config, the filesystem, history or any backup. Callers get the same
        validation errors they would get from a real run.
        """
        plan = self.plan()
        if not targets or not {t.strip() for t in targets if t.strip()}:
            raise UpdateError(
                "missing_targets",
                t("upd_targets_required"),
                t("upd_targets_hint"),
            )
        wanted = {t.strip() for t in targets if t.strip()}
        selected = [e for e in plan.targets if not wanted or e.name in wanted]
        missing = wanted - {e.name for e in selected}
        result = UpdateResult(action="apply")
        for name in sorted(missing):
            result.errors.append(t("upd_no_target", name=name))

        txn = _Txn(self.home, "dry-run" if dry_run else "apply",
                   restore_tree=self._restore_tree)
        committed: list[CatalogEntry] = []
        # In-memory counterpart of the config backup: rolling back config.toml on
        # disk while the live AppConfig keeps the new entries would leave the
        # running TUI and the file disagreeing.
        providers_before = dict(getattr(self.cfg, "providers", {}) or {})
        models_before = dict(getattr(self.cfg, "models", {}) or {})
        with contextlib.ExitStack() as stack:
            if not dry_run:
                stack.enter_context(self._apply_lock())
            for entry in selected:
                try:
                    if entry.kind == "core":
                        await self._apply_core(entry, dry_run, result, txn)
                    elif entry.kind == "plugin":
                        await self._apply_plugin(entry, dry_run, result, txn)
                    elif entry.kind == "mcp":
                        self._apply_mcp(entry, dry_run, result, txn)
                    elif entry.kind in ("provider", "model"):
                        self._apply_config(entry, dry_run, result, txn)
                    else:
                        raise UpdateError("unsupported_kind",
                                          t("upd_kind_not_implemented", name=entry.name, kind=entry.kind))
                    committed.append(entry)
                except UpdateError as exc:
                    result.errors.append(f"{exc.code}: {exc}")
                    if dry_run:
                        continue  # preview reports every problem, mutates nothing
                    rolled_back = [e.name for e in committed]
                    result.entries = []  # nothing survived: the txn is all-or-nothing
                    result.errors.extend(txn.restore())
                    if hasattr(self.cfg, "providers"):
                        self.cfg.providers = providers_before
                    if hasattr(self.cfg, "models"):
                        self.cfg.models = models_before
                    result.errors.append(
                        t("upd_txn_rolled", txn=txn.id, count=len(rolled_back))
                        + t("upd_txn_clean", names='、'.join(rolled_back) or t("upd_none")))
                    break
                except BaseException:
                    # Cancellation or an unexpected error: never leave a
                    # partially installed transaction behind, then re-raise.
                    if not dry_run and txn.steps:
                        result.entries = []
                        result.errors.extend(txn.restore())
                        if hasattr(self.cfg, "providers"):
                            self.cfg.providers = providers_before
                        if hasattr(self.cfg, "models"):
                            self.cfg.models = models_before
                        result.errors.append(
                            t("upd_txn_interrupted", txn=txn.id))
                        txn.write_manifest()
                    raise

        if not dry_run:
            txn.write_manifest()
            if txn.steps:
                self._append_history({
                    "action": "txn", "txn": txn.id,
                    "targets": [e.name for e in committed],
                    "attempted": [e.name for e in selected],
                    "failed": len(result.errors), "dir": str(txn.dir),
                })
        if not dry_run and result.entries:
            self._append_history({
                "action": "apply_summary", "applied": [e.name for e in result.entries],
                "failed": len(result.errors),
            })
        return result

    # ----------------------------------------------------- per-kind transactions

    MAX_CORE_BYTES = 200 * 1024 * 1024  # core bundles are allowed to be big

    def _core_package_dir(self) -> Path:
        """The live oaset package directory. Source/editable checkouts are
        REFUSED: overwriting a development tree is never a transaction."""
        import oaset

        pkg_dir = Path(oaset.__file__).parent
        if self.package_dir is not None:
            return self.package_dir  # injected (tests / advanced users): no guard
        if pkg_dir.parent.name == "src" or (pkg_dir.parent / "pyproject.toml").is_file():
            raise UpdateError(
                "source_checkout",
                t("upd_source_checkout", pkg_dir=pkg_dir),
            )
        return pkg_dir

    async def _apply_core(self, entry: CatalogEntry, dry_run: bool,
                          result: UpdateResult, txn: _Txn) -> None:
        """Transactional core upgrade over a zip bundle release.

        download → sha256 verify → zip-slip guard → stage → validate layout →
        backup package dir → file-level replace → history; ANY failure
        restores the previous tree from the backup before raising.
        """
        import io
        import shutil
        import zipfile

        if not entry.sha256:
            raise UpdateError("verification_failed",
                              t("upd_missing_sha", name=entry.name))
        payload = await self._download(entry.source_url, self.MAX_CORE_BYTES, entry.name)
        digest = hashlib.sha256(payload).hexdigest()
        if digest.lower() != entry.sha256.lower():
            raise UpdateError("verification_failed",
                              t("upd_sha_mismatch", name=entry.name, want=entry.sha256[:12], got=digest[:12]))
        self._verify_artifact_signature(entry, payload)

        dest_pkg = self._core_package_dir()
        staging = Path(tempfile.mkdtemp(prefix="oaset-core-stage-"))
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
                for member in bundle.namelist():
                    member_path = Path(member)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise UpdateError("unsafe_target",
                                          t("upd_unsafe_path", name=entry.name, member=member))
                bundle.extractall(staging)
            staged_pkg = staging / "oaset"
            if not (staged_pkg / "__init__.py").is_file():
                raise UpdateError("catalog_schema",
                                  t("upd_bad_core_zip", name=entry.name))
        except UpdateError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except zipfile.BadZipFile as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise UpdateError("verification_failed",
                              t("upd_invalid_zip", name=entry.name, exc=exc)) from exc

        if dry_run:
            # staged and validated, but nothing is backed up or replaced
            shutil.rmtree(staging, ignore_errors=True)
            result.entries.append(entry)
            result.notes.append(t("upd_dryrun_core", name=entry.name, version=entry.version))
            return

        backup_dir = (self.home / "update" / "core-backup"
                      / f"{_safe_segment(entry.name)}-{_safe_segment(entry.version)}-{int(time.time())}-{os.urandom(2).hex()}")
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(dest_pkg, backup_dir)
        core_step = txn.record(dest_pkg, str(backup_dir), created=False, tree=True)
        core_step["target"] = entry.name
        try:
            staged_files: set[Path] = set()
            for src in sorted(staged_pkg.rglob("*")):
                if src.is_dir():
                    continue
                rel = src.relative_to(staged_pkg)
                staged_files.add(rel)
                dst = dest_pkg / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                _copy_file(src, dst)
            # mirror the release tree: remove stale modules the new release
            # dropped (prevents ghost imports of old-version code)
            for p in dest_pkg.rglob("*"):
                if p.is_file() and p.relative_to(dest_pkg) not in staged_files:
                    with contextlib.suppress(OSError):
                        p.unlink()
        except OSError as exc:
            self._restore_tree(dest_pkg, backup_dir)
            raise UpdateError("apply_failed",
                              t("upd_core_replace_failed", name=entry.name, exc=exc)) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self._append_history({
            "action": "apply", "target": entry.name, "kind": entry.kind,
            "version": entry.version, "sha256": digest,
            "source_url": entry.source_url, "dest": str(dest_pkg),
            "backup": str(backup_dir), "created": False, "txn": txn.id,
        })
        result.notes.append(
            t("upd_core_done", name=entry.name, version=entry.version, backup=backup_dir.name))
        result.entries.append(entry)

    def _restore_tree(self, dest_pkg: Path, backup_dir: Path) -> None:
        """Restore `dest_pkg` from `backup_dir`: copy every backed-up file
        back, then remove files created after the backup."""
        backup_files = {p.relative_to(backup_dir) for p in backup_dir.rglob("*") if p.is_file()}
        for rel in sorted(backup_files):
            dst = dest_pkg / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(backup_dir / rel, dst)
        current = {p.relative_to(dest_pkg) for p in dest_pkg.rglob("*") if p.is_file()}
        for rel in sorted(current - backup_files):
            with contextlib.suppress(OSError):
                (dest_pkg / rel).unlink()

    def _backup_file(self, path: Path) -> tuple[str | None, bool]:
        """Deprecated sibling-`.bak` backup, kept for external callers/tests.

        Internal transactions use `_Txn.stage`, which never collides with a
        second target writing to the same destination.
        """
        if path.exists():
            backup = Path(str(path) + ".bak")
            backup.write_bytes(path.read_bytes())
            return str(backup), False
        return None, True

    async def _apply_plugin(self, entry: CatalogEntry, dry_run: bool,
                            result: UpdateResult, txn: _Txn) -> None:
        dest = self._plugin_dest(entry.name)
        if not entry.sha256:
            raise UpdateError("verification_failed",
                              t("upd_entry_missing_sha", name=entry.name))
        payload = await self._download(entry.source_url, MAX_PACKAGE_BYTES, entry.name)
        digest = hashlib.sha256(payload).hexdigest()
        if digest.lower() != entry.sha256.lower():
            raise UpdateError("verification_failed",
                              t("upd_sha_mismatch", name=entry.name, want=entry.sha256[:12], got=digest[:12]))
        self._verify_artifact_signature(entry, payload)
        if dry_run:
            result.entries.append(entry)
            result.notes.append(t("upd_dryrun_plugin", name=entry.name, version=entry.version, dest=dest))
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        step = txn.stage(dest)
        step["target"] = entry.name
        backup, created = step["backup"], step["created"]
        try:
            _write_bytes_locked(dest, payload)
        except OSError as exc:
            _restore_file(dest, backup, created)
            raise UpdateError("apply_failed", t("upd_plugin_write_failed", name=entry.name, exc=exc)) from exc
        self._append_history({
            "action": "apply", "target": entry.name, "kind": entry.kind,
            "version": entry.version, "sha256": digest,
            "source_url": entry.source_url, "dest": str(dest),
            "backup": backup, "created": created, "txn": txn.id,
        })
        result.notes.append(
            t("upd_plugin_done", name=entry.name, version=entry.version))
        result.entries.append(entry)

    def _apply_mcp(self, entry: CatalogEntry, dry_run: bool,
                   result: UpdateResult, txn: _Txn) -> None:
        install = entry.install or {}
        if install.get("url"):
            spec: dict[str, Any] = {"url": str(install["url"])}
            if install.get("headers"):
                spec["headers"] = {str(k): str(v) for k, v in install["headers"].items()}
        elif install.get("command"):
            spec = {"command": str(install["command"]),
                    "args": [str(a) for a in (install.get("args") or [])]}
            if install.get("env"):
                spec["env"] = {str(k): str(v) for k, v in install["env"].items()}
        else:
            raise UpdateError("catalog_schema",
                              t("upd_mcp_missing_install", name=entry.name))
        path = self.home / "mcp.json"
        data: dict[str, Any] = {}
        if path.exists():
            with contextlib.suppress(ValueError):
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise UpdateError("apply_failed", t("upd_mcp_bad_servers", name=entry.name))
        if dry_run:
            # validated against the real file, but the file itself is untouched
            result.entries.append(entry)
            result.notes.append(t("upd_dryrun_mcp", name=entry.name, version=entry.version))
            return
        step = txn.stage(path)
        step["target"] = entry.name
        backup, created = step["backup"], step["created"]
        previous_server = servers.get(entry.name)
        step["key_edits"] = [{
            "file": str(path),
            "path": ["mcpServers", entry.name],
            "previous": previous_server if previous_server is not None else None,
        }]
        servers[entry.name] = spec
        try:
            _write_json_atomic(path, data)
        except OSError as exc:
            _restore_file(path, backup, created)
            raise UpdateError("apply_failed", t("upd_mcp_write_failed", name=entry.name, exc=exc)) from exc
        self._append_history({
            "action": "apply", "target": entry.name, "kind": entry.kind,
            "version": entry.version, "dest": str(path),
            "backup": backup, "created": created, "txn": txn.id,
        })
        result.notes.append(t("upd_mcp_done", name=entry.name, version=entry.version))
        result.entries.append(entry)

    def _config_entry_update(self, entry: CatalogEntry) -> tuple[str, Any, str]:
        """Validate one provider/model entry and build the value to insert.

        Pure: it never touches ``self.cfg``, so a dry run can validate without
        polluting the live configuration the running TUI shares.
        """
        from oaset.config import ModelConfig, ProviderConfig

        install = entry.install or {}
        if entry.kind == "provider" and not str(install.get("base_url", "")).strip():
            raise UpdateError("catalog_schema", t("upd_provider_missing_url", name=entry.name))
        if entry.kind == "model" and not str(install.get("provider", "")).strip():
            raise UpdateError("catalog_schema", t("upd_model_missing_provider", name=entry.name))
        try:
            if entry.kind == "provider":
                name = str(install.get("name") or entry.name)
                provider_value = ProviderConfig(
                    name=name,
                    type=str(install.get("type", "openai")),
                    api_key=str(install.get("api_key", "")),
                    base_url=str(install.get("base_url", "")),
                    prompt_cache=bool(install.get("prompt_cache", True)),
                )
                return name, provider_value, t("upd_provider_added", name=name)
            mid = str(install.get("id") or entry.name)
            model_value = ModelConfig(
                id=mid,
                provider=str(install.get("provider", "")),
                model=str(install.get("model", mid.split("/")[-1])),
                max_context_size=int(install.get("max_context_size", 128000)),
                max_output_size=int(install.get("max_output_size", 32768)),
                capabilities=[str(c) for c in (install.get("capabilities") or ["tool_use"])],
                display_name=str(install.get("display_name", mid)),
                reasoning_key=install.get("reasoning_key"),
            )
            return mid, model_value, t("upd_model_added", mid=mid)
        except (TypeError, ValueError) as exc:
            raise UpdateError("catalog_schema", t("upd_install_invalid", name=entry.name, exc=exc)) from exc

    def _apply_config(self, entry: CatalogEntry, dry_run: bool,
                      result: UpdateResult, txn: _Txn) -> None:
        """provider/model: merge into the live AppConfig and save_config().

        Adds entries only — default_model/default_provider are never touched:
        enabling an unverified model requires explicit user action.

        Registration order matters: the entry is validated and *built* first,
        the live config is mutated only after we are committed to writing, and
        a failed write rolls the in-memory config back too (a config that only
        exists in memory while the file says otherwise is a silent corruption).
        """
        from oaset.config import config_path, save_config

        name, value, note = self._config_entry_update(entry)
        if dry_run:
            # nothing mutated: not self.cfg, not config.toml, not history
            result.entries.append(entry)
            result.notes.append(t("upd_dryrun_cfg", name=entry.name, version=entry.version, note=note))
            return

        path = config_path()
        container = "providers" if entry.kind == "provider" else "models"
        target = self.cfg.providers if entry.kind == "provider" else self.cfg.models
        replaced = name in target
        previous = target.get(name)
        step = txn.stage(path)
        step["target"] = entry.name
        backup, created = step["backup"], step["created"]
        step["key_edits"] = [{
            "file": str(path),
            "path": [container, name],
            "previous": asdict(previous) if replaced and previous is not None else None,
        }]
        target[name] = value
        try:
            save_config(self.cfg)
        except Exception as exc:
            if replaced:
                target[name] = previous
            else:
                target.pop(name, None)
            _restore_file(path, backup, created)
            raise UpdateError("apply_failed", t("upd_cfg_write_failed", name=entry.name, exc=exc)) from exc
        self._append_history({
            "action": "apply", "target": entry.name, "kind": entry.kind,
            "version": entry.version, "dest": str(path),
            "backup": backup, "created": created, "txn": txn.id,
        })
        result.notes.append(f"{entry.name} v{entry.version}: {note}。")
        result.entries.append(entry)

    def _rollback_key_edits(self, edits: list[dict[str, Any]]) -> list[str]:
        """Undo recorded key-level edits, leaving sibling/extra keys intact.

        Used when a transaction touched a shared file (config.toml / mcp.json)
        so rolling back ONE target does not revert the other targets the same
        apply wrote, nor edits the user made afterwards.
        """
        problems: list[str] = []
        by_file: dict[str, list[dict[str, Any]]] = {}
        for edit in edits:
            by_file.setdefault(str(edit.get("file") or ""), []).append(edit)
        for file_name, file_edits in by_file.items():
            if not file_name:
                problems.append(t("upd_rollback_missing_name"))
                continue
            if file_name.endswith(".json"):
                problems.extend(_undo_json_key_edits(Path(file_name), file_edits))
            else:
                problems.extend(_undo_toml_key_edits(Path(file_name), file_edits))
        return problems

    def _txn_manifest(self, txn_id: str) -> dict[str, Any] | None:
        if not txn_id or "/" in txn_id or "\\" in txn_id or ".." in txn_id:
            return None
        path = self.home / "update" / "txn" / txn_id / "manifest.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _rollback_txn(self, manifest: dict[str, Any], chosen: dict[str, Any],
                      result: UpdateResult) -> UpdateResult:
        """Restore a whole multi-target transaction in reverse order.

        A transaction is the atomic unit: its targets may share one file
        (config.toml, mcp.json), so undoing a single step of it would leave a
        state that never existed. The note names every target that was undone.
        """
        txn = _Txn.from_manifest(self.home, manifest, restore_tree=self._restore_tree)
        targets = [str(r.get("target")) for r in self.history(limit=500)
                   if r.get("action") == "apply" and r.get("txn") == txn.id]
        problems = txn.restore()
        result.errors.extend(problems)
        self._append_history({
            "action": "rollback", "txn": txn.id, "targets": targets,
            "requested": chosen.get("target"), "failed": len(problems),
        })
        if problems:
            result.errors.append(t("upd_partial_rollback"))
        else:
            result.notes.append(
                t("upd_rollback_done_a", txn=txn.id, count=len(txn.steps))
                + (t("upd_rollback_done_targets", targets='、'.join(t for t in targets if t)) if any(targets) else "")
                + t("upd_rollback_done_tail"))
        return result

    async def rollback(self, target: str = "last") -> UpdateResult:
        """Restore the recorded pre-apply state for `target`.

        `target` is "last" (most recent apply), a target name, or "txn:<id>".
        A single-target transaction restores just that step's backup; a
        multi-target transaction is restored as a whole in reverse order. Uses
        the unified {dest, backup, created} history record, so plugins,
        mcp.json and config.toml all roll back the same way. Records history.
        """
        result = UpdateResult(action="rollback")
        records = [r for r in self.history(limit=200) if r.get("action") == "apply"]
        if not records:
            result.errors.append(t("upd_no_rollback_history"))
            return result
        chosen: dict[str, Any] | None
        if target in ("last", ""):
            chosen = records[-1]
        elif target.startswith("txn:"):
            txn_id = target[4:].strip()
            matches = [r for r in reversed(records) if r.get("txn") == txn_id]
            chosen = matches[0] if matches else None
            if chosen is None:
                result.errors.append(t("upd_no_txn_record", txn=txn_id))
                return result
        else:
            matches = [r for r in reversed(records) if r.get("target") == target]
            chosen = matches[0] if matches else None
            if chosen is None:
                result.errors.append(t("upd_no_target_record", target=target))
                return result
        txn_id = str(chosen.get("txn") or "")
        manifest = self._txn_manifest(txn_id) if txn_id else None
        # key-level undo first: a shared config.toml / mcp.json touched by
        # several targets must not lose its siblings when one target rolls back
        steps = (manifest or {}).get("steps") or []
        edits: list[dict[str, Any]] = []
        for step in steps:
            if str(step.get("dest") or "") != str(chosen.get("dest") or ""):
                continue
            # only the chosen target's own edits: siblings in the same file stay
            if step.get("target") and str(step["target"]) != str(chosen.get("target")):
                continue
            edits.extend(step.get("key_edits") or [])
        if edits:
            problems = self._rollback_key_edits(edits)
            result.errors.extend(problems)
            if not problems:
                self._append_history({
                    "action": "rollback", "target": chosen.get("target"),
                    "from_version": chosen.get("version"), "txn": txn_id,
                    "mode": "keys",
                })
                result.notes.append(
                    t("upd_rollback_keys_head", target=chosen.get('target'), version=chosen.get('version'))
                    + t("upd_key_only_note"))
            return result
        if manifest and len(steps) > 1:
            return self._rollback_txn(manifest, chosen, result)
        dest = str(chosen.get("dest", ""))
        backup = chosen.get("backup")
        created = bool(chosen.get("created"))
        if not dest:
            result.errors.append(t("upd_record_missing_dest"))
            return result
        if chosen.get("kind") == "core":
            # directory-level restore: copy the backup tree back over the
            # package dir, removing files created after the backup
            if not backup or not Path(backup).is_dir():
                result.errors.append(t("upd_core_backup_missing"))
                return result
            try:
                self._restore_tree(Path(dest), Path(backup))
            except OSError as exc:
                result.errors.append(t("upd_core_rollback_failed", exc=exc))
                return result
            self._append_history({
                "action": "rollback", "target": chosen.get("target"),
                "from_version": chosen.get("version"),
            })
            result.notes.append(t("upd_core_rollback_done",
                                  target=chosen.get("target"),
                                  version=chosen.get("version")))
            return result
        if backup is None and not created:
            # legacy record from before unified backups: plugin-style sibling .bak
            sibling = Path(str(dest) + ".bak")
            backup = str(sibling) if sibling.exists() else None
        try:
            _restore_file(dest, backup, created)
        except OSError as exc:
            result.errors.append(t("upd_rollback_failed_generic", exc=exc))
            return result
        self._append_history({
            "action": "rollback", "target": chosen.get("target"),
            "from_version": chosen.get("version"),
        })
        result.notes.append(t("upd_rollback_done", target=chosen.get("target"),
                              version=chosen.get("version")))
        return result


def verify_sha256(path: Path, expected: str) -> bool:
    """Constant-purpose digest check used by (future) apply and by tests."""
    if not expected:
        return False
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest.lower() == expected.lower()
