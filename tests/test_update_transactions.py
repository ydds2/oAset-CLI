"""UPD-01 regressions: HTTP session lifecycle, dry-run purity, transactional
multi-target apply (staging + manifest + reverse-order recovery) and the
single-writer apply lock.

The pre-fix defects these lock down (all statically verified in the review):
  * `self._fetch = client.get` captured inside `async with httpx.AsyncClient()`
    and awaited after the block exited → every request used a closed client;
  * `_apply_config` mutated the live `AppConfig` and then returned on the
    dry-run branch → dry-run polluted the process-wide shared config;
  * per-destination sibling `.bak` backups were overwritten by a second target
    writing the same file, and a mid-transaction failure left partial installs.
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from oaset.update import (
    CatalogEntry,
    FetchSession,
    UpdateError,
    UpdateManager,
    _Txn,
)


class Cfg:
    network_mode = "pull_only"


class LocalCfg:
    network_mode = "local_only"


class FakeResponse:
    def __init__(self, text: str = "", content: bytes | None = None):
        self.text = text
        if content is not None:
            self.content = content


def _catalog(entries: list[dict]) -> str:
    return json.dumps({"entries": entries})


def _plugin(entry_name: str, version: str, payload: bytes) -> dict:
    return {
        "kind": "plugin", "name": entry_name, "version": version,
        "source_url": f"https://cdn.test/{entry_name}-{version}.py",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


# ------------------------------------------------------------------ fetch session


async def test_fetch_session_never_closes_an_injected_fetch():
    calls: list[str] = []

    async def injected(url):
        calls.append(url)
        return FakeResponse("{}")

    session = FetchSession(injected)
    assert session.owns_client is False
    async with session as live:
        assert (await live.get("https://example.test/x")).text == "{}"
        assert (await live.get("https://example.test/y")).text == "{}"
    assert calls == ["https://example.test/x", "https://example.test/y"]
    # an injected callable stays usable after the block: we never owned it
    assert session.closed is False


async def test_fetch_session_refuses_use_after_close():
    """The exact pre-fix bug: a request issued after the client closed."""
    session = FetchSession(None)  # owns a real httpx client
    async with session:
        assert session.closed is False
    assert session.closed is True
    with pytest.raises(UpdateError) as excinfo:
        await session.get("https://example.test/x")
    assert excinfo.value.code == "fetch_closed"


async def test_fetch_session_rejects_non_http_sources():
    async def injected(url):  # pragma: no cover - must never be reached
        raise AssertionError("unsafe scheme must be refused before any request")

    session = FetchSession(injected)
    async with session as live:
        for bad in ("file:///etc/passwd", "ftp://host/p", "C:\\Windows", ""):
            with pytest.raises(UpdateError) as excinfo:
                await live.get(bad)
            assert excinfo.value.code == "unsafe_source"


async def test_refresh_does_not_leak_or_replace_the_fetch_callable(tmp_path):
    """After refresh, `_fetch` must still be the injected value (or None) —
    never a bound method of an AsyncClient that has already been closed."""
    payload = _catalog([{"kind": "plugin", "name": "demo", "version": "1"}])

    async def fetch(url):
        return FakeResponse(payload)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)
    await mgr.refresh()
    assert mgr._fetch is fetch


async def test_refresh_without_injection_keeps_fetch_unset(tmp_path, monkeypatch):
    """With no injected fetch the manager must not cache a client-bound method."""
    payload = _catalog([{"kind": "plugin", "name": "demo", "version": "1"}])

    class _Response:
        text = payload
        content = payload.encode()

    class _Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False

        async def get(self, url):
            return _Response()

        async def aclose(self):
            self.closed = True

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    mgr = UpdateManager(Cfg(), home=tmp_path)
    await mgr.refresh()
    assert mgr._fetch is None


async def test_apply_refuses_unsafe_package_url(tmp_path):
    payload = b"print('x')"
    entry = _plugin("evil", "1", payload)
    entry["source_url"] = "file:///etc/passwd"

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_catalog([entry]))
        raise AssertionError(f"unsafe url must not be requested: {url}")

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)
    await mgr.refresh()
    result = await mgr.apply(["evil"])
    assert any("unsafe_source" in err for err in result.errors), result.errors
    assert not (tmp_path / "plugins" / "evil.py").exists()


# ------------------------------------------------------------------- dry-run purity


async def test_dry_run_does_not_mutate_live_config_or_file(tmp_path, monkeypatch):
    """Pre-fix: _apply_config mutated self.cfg.providers/models before the
    dry-run branch returned, polluting the config shared with the running TUI."""
    import oaset.config as cfgmod
    from oaset.config import default_config, load_config

    cfg_file = tmp_path / "config.toml"
    monkeypatch.setattr(cfgmod, "config_path", lambda: cfg_file)
    cfg = default_config()
    cfgmod.save_config(cfg)
    before_providers = dict(cfg.providers)
    before_models = dict(cfg.models)
    before_bytes = cfg_file.read_bytes()

    async def fetch(url):
        return FakeResponse(_catalog([
            {"kind": "provider", "name": "acme", "version": "1",
             "install": {"name": "acme", "type": "openai",
                         "base_url": "https://api.acme.test/v1"}},
            {"kind": "model", "name": "acme/turbo", "version": "1",
             "install": {"id": "acme/turbo", "provider": "acme", "model": "turbo"}},
        ]))

    mgr = UpdateManager(cfg, home=tmp_path, fetch=fetch)
    await mgr.refresh()
    result = await mgr.apply(["acme", "acme/turbo"], dry_run=True)

    assert result.errors == [], result.errors
    assert set(cfg.providers) == set(before_providers), "dry-run polluted providers"
    assert set(cfg.models) == set(before_models), "dry-run polluted models"
    assert cfg_file.read_bytes() == before_bytes
    assert any("[dry-run]" in note for note in result.notes)
    reloaded = load_config()
    assert "acme" not in reloaded.providers


async def test_dry_run_writes_no_history_and_no_backups(tmp_path):
    payload = b"print('dry')"
    entry = _plugin("demo", "1", payload)

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_catalog([entry]))
        return FakeResponse(content=payload)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)
    await mgr.refresh()
    result = await mgr.apply(["demo"], dry_run=True)
    assert result.errors == []
    assert mgr.history() == []
    assert not (tmp_path / "update" / "txn").exists()
    assert not (tmp_path / "update" / "apply.lock").exists()
    assert not (tmp_path / "plugins" / "demo.py").exists()
    assert not (tmp_path / "plugins" / "demo.py.bak").exists()


async def test_dry_run_reports_every_failing_target(tmp_path):
    """A preview must list all problems instead of stopping at the first."""
    good = b"ok"
    entries = [
        {"kind": "plugin", "name": "nohash", "version": "1",
         "source_url": "https://cdn.test/nohash.py"},
        _plugin("badhash", "1", b"expected"),
        _plugin("fine", "1", good),
    ]

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_catalog(entries))
        if url.endswith("badhash-1.py"):
            return FakeResponse(content=b"tampered")
        return FakeResponse(content=good)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)
    await mgr.refresh()
    result = await mgr.apply(["nohash", "badhash", "fine"], dry_run=True)
    joined = " ".join(result.errors)
    assert "sha256" in joined
    assert "badhash" in joined
    assert [e.name for e in result.entries] == ["fine"]
    assert not (tmp_path / "plugins").exists()


# ------------------------------------------------------- transactional multi-target


async def test_multi_target_failure_restores_committed_targets_in_reverse(tmp_path):
    first, second = b"v1 body", b"v2 body"
    entries = [_plugin("alpha", "1", first), _plugin("beta", "1", second)]

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_catalog(entries))
        if url.endswith("beta-1.py"):
            raise OSError("connection reset (injected)")
        return FakeResponse(content=first)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)
    await mgr.refresh()
    result = await mgr.apply(["alpha", "beta"])

    assert any("fetch_failed" in err for err in result.errors), result.errors
    assert any("逆序回滚" in err for err in result.errors)
    # alpha was committed, then rolled back: no partial install survives
    assert result.entries == []
    assert not (tmp_path / "plugins" / "alpha.py").exists()
    assert not (tmp_path / "plugins" / "beta.py").exists()
    assert not (tmp_path / "plugins" / "alpha.py.bak").exists()


async def test_multi_target_failure_restores_previous_file_content(tmp_path):
    """A failing second target must put an already-upgraded file back."""
    old, new = b"print('old')", b"print('new')"
    dest = tmp_path / "plugins" / "alpha.py"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(old)

    entries = [_plugin("alpha", "2", new), _plugin("beta", "1", b"b")]

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_catalog(entries))
        if url.endswith("beta-1.py"):
            raise OSError("boom")
        return FakeResponse(content=new)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)
    await mgr.refresh()
    result = await mgr.apply(["alpha", "beta"])
    assert result.errors
    assert dest.read_bytes() == old, "alpha was left on the new version"


async def test_transaction_manifest_records_steps(tmp_path):
    payload = b"print('m')"
    entry = _plugin("demo", "1", payload)

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_catalog([entry]))
        return FakeResponse(content=payload)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)
    await mgr.refresh()
    await mgr.apply(["demo"])

    txn_dirs = list((tmp_path / "update" / "txn").glob("*"))
    assert len(txn_dirs) == 1
    manifest = json.loads((txn_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["label"] == "apply"
    assert [s["dest"] for s in manifest["steps"]] == [str(tmp_path / "plugins" / "demo.py")]
    assert manifest["steps"][0]["created"] is True  # the file did not exist before

    record = json.loads((tmp_path / "update" / "history.jsonl")
                        .read_text(encoding="utf-8").splitlines()[0])
    assert record["action"] == "apply" and record["txn"] == manifest["txn"]


async def test_each_target_gets_its_own_backup_not_a_shared_sibling(tmp_path, monkeypatch):
    """config.toml touched by two targets must keep two distinct backups, so
    rolling back the first restores its own pre-state, not the second's."""
    import oaset.config as cfgmod
    from oaset.config import default_config

    real_path = tmp_path / "cfgdir" / "config.toml"
    real_path.parent.mkdir()
    monkeypatch.setattr(cfgmod, "config_path", lambda: real_path)
    cfg = default_config()
    cfgmod.save_config(cfg)
    original = real_path.read_bytes()

    async def fetch(url):
        return FakeResponse(_catalog([
            {"kind": "provider", "name": "acme", "version": "1",
             "install": {"name": "acme", "type": "openai",
                         "base_url": "https://api.acme.test/v1"}},
            {"kind": "provider", "name": "other", "version": "1",
             "install": {"name": "other", "type": "openai",
                         "base_url": "https://api.other.test/v1"}},
        ]))

    mgr = UpdateManager(cfg, home=tmp_path, fetch=fetch)
    await mgr.refresh()
    result = await mgr.apply(["acme", "other"])
    assert result.errors == [], result.errors
    assert not (real_path.parent / "config.toml.bak").exists(), \
        "shared sibling .bak must not be used by the transaction"

    backups = sorted((tmp_path / "update" / "txn").glob("*/0*.bak"))
    assert len(backups) == 2, [str(b) for b in backups]
    assert backups[0].read_bytes() == original
    assert b"acme" in backups[1].read_bytes()  # state after the first write

    # rolling back ONE target must undo only that target's key: the sibling
    # provider added by the same transaction survives
    rb_first = await mgr.rollback("acme")
    assert any("已回滚" in n for n in rb_first.notes), rb_first.errors
    after = real_path.read_text(encoding="utf-8")
    assert "other" in after, "key-level rollback must not drop the sibling target"
    assert "api.acme.test" not in after, "the rolled-back target must be gone"


async def test_apply_lock_rejects_concurrent_transaction(tmp_path):
    payload = b"p"
    entry = _plugin("demo", "1", payload)

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_catalog([entry]))
        return FakeResponse(content=payload)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)
    await mgr.refresh()
    lock = tmp_path / "update" / "apply.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(time.time()), encoding="utf-8")

    with pytest.raises(UpdateError) as excinfo:
        await mgr.apply(["demo"])
    assert excinfo.value.code == "apply_busy"
    assert not (tmp_path / "plugins" / "demo.py").exists()
    assert lock.exists(), "a refused apply must not remove someone else's lock"


async def test_stale_apply_lock_is_reclaimed(tmp_path):
    from oaset.update import APPLY_LOCK_STALE_SECONDS

    payload = b"p"
    entry = _plugin("demo", "1", payload)

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_catalog([entry]))
        return FakeResponse(content=payload)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)
    await mgr.refresh()
    lock = tmp_path / "update" / "apply.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(time.time() - APPLY_LOCK_STALE_SECONDS - 5), encoding="utf-8")

    result = await mgr.apply(["demo"])
    assert result.errors == [], result.errors
    assert (tmp_path / "plugins" / "demo.py").exists()
    assert not lock.exists(), "lock must be released even after being reclaimed"


async def test_failed_apply_still_releases_the_lock(tmp_path):
    async def fetch(url):
        return FakeResponse(_catalog([{"kind": "plugin", "name": "nohash", "version": "1",
                                       "source_url": "https://cdn.test/nohash.py"}]))

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)
    await mgr.refresh()
    result = await mgr.apply(["nohash"])
    assert result.errors
    assert not (tmp_path / "update" / "apply.lock").exists()


# ------------------------------------------------------------------ txn internals


def test_txn_restore_reports_unrestorable_steps_instead_of_raising(tmp_path, monkeypatch):
    """A rollback that cannot write must surface a diagnostic, not abort the
    remaining (reverse-order) steps."""
    from oaset import update as upd

    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a0", encoding="utf-8")
    b.write_text("b0", encoding="utf-8")
    txn = _Txn(tmp_path)
    step_a = txn.stage(a)
    a.write_text("a1", encoding="utf-8")
    txn.stage(b)
    b.write_text("b1", encoding="utf-8")

    real_restore = upd._restore_file
    seen: list[str] = []

    def flaky(dest, backup, created):
        seen.append(str(dest))
        if str(dest).endswith("a.txt"):
            raise OSError("disk full (injected)")
        real_restore(dest, backup, created)

    monkeypatch.setattr(upd, "_restore_file", flaky)
    problems = txn.restore()
    assert step_a["seq"] == 1
    assert seen == [str(b), str(a)], "steps must be restored in reverse order"
    assert b.read_text(encoding="utf-8") == "b0"  # the later step still recovered
    assert len(problems) == 1 and "a.txt" in problems[0]


async def test_core_dry_run_reports_source_checkout_blocker(tmp_path):
    """A dry run must surface the same blocker a real apply would hit."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("oaset/__init__.py", '__version__ = "999"\n')
    payload = buffer.getvalue()

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_catalog([{
                "kind": "core", "name": "oaset-cli", "version": "999.0.0",
                "source_url": "https://cdn.test/oaset.zip",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }]))
        return FakeResponse(content=payload)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)
    await mgr.refresh()
    result = await mgr.apply(["oaset-cli"], dry_run=True)
    # running from a source checkout: the preview must say so, not claim success
    assert any("源码/可编辑安装" in err for err in result.errors), result.errors
    assert not (tmp_path / "update" / "core-backup").exists()


def test_catalog_entry_kind_still_validated():
    with pytest.raises(UpdateError):
        CatalogEntry.from_dict({"kind": "nope", "name": "x", "version": "1"})


def test_txn_directories_are_unique_per_run(tmp_path):
    """Two applies in the same second must not share a staging directory."""
    ids = {_Txn(tmp_path).id for _ in range(5)}
    assert len(ids) == 5
    assert all(t.dir.parent == tmp_path / "update" / "txn" for t in [_Txn(tmp_path)])
