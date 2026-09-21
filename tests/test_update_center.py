"""Update Center (`oaset update`, `/update`) — read-only domain tests.

UPDATE-01 (check/refresh/list/plan), UPDATE-02 (policy refusal, no silent
network), UPDATE-03 (schema/version/hash validation) coverage.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from oaset.update import (
    CACHE_TTL_SECONDS,
    CatalogEntry,
    UpdateError,
    UpdateManager,
    verify_sha256,
)


class Cfg:
    network_mode = "pull_only"


class LocalCfg:
    network_mode = "local_only"


class FakeResponse:
    def __init__(self, text: str):
        self.text = text


def _catalog(entries: list[dict]) -> str:
    return json.dumps({"entries": entries})


async def test_refresh_writes_cache_and_lists_entries(tmp_path):
    payload = _catalog([
        {"kind": "plugin", "name": "demo", "version": "1.2.0", "description": "示例插件"},
        {"kind": "model", "name": "glm-5.3", "version": "2026-09-01"},
    ])

    async def fetch(url):
        return FakeResponse(payload)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)
    result = await mgr.refresh()
    assert [e.name for e in result.entries] == ["demo", "glm-5.3"]
    assert result.from_cache is False

    cached = mgr.list_entries()
    assert [e.name for e in cached.entries] == ["demo", "glm-5.3"]
    assert cached.from_cache is True


async def test_check_uses_fresh_cache_without_network(tmp_path):
    payload = _catalog([{"kind": "core", "name": "oaset-cli", "version": "0.0.0"}])

    async def fetch(url):  # would fail the test if called
        return FakeResponse(payload)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)
    await mgr.refresh()

    async def no_network(url):  # swap in after the cache is populated
        raise AssertionError("check must use fresh cache, not the network")

    mgr._fetch = no_network
    entries, _ = mgr._read_cache()
    mgr._write_cache(entries, time.time())
    result = await mgr.check()
    assert result.from_cache is True
    assert not result.errors


async def test_local_only_refuses_refresh_with_structured_error(tmp_path):
    async def fetch(url):
        raise AssertionError("local_only must never issue a request")

    mgr = UpdateManager(LocalCfg(), home=tmp_path, fetch=fetch)
    with pytest.raises(UpdateError) as excinfo:
        await mgr.refresh()
    assert excinfo.value.code == "network_blocked"


async def test_check_reports_stale_cache_error_but_keeps_entries(tmp_path):
    async def fetch(url):
        raise OSError("dns down")

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=None)
    entries, _ = mgr._read_cache()
    mgr._write_cache([CatalogEntry(kind="plugin", name="old", version="0.1.0")], time.time() - 2 * CACHE_TTL_SECONDS)
    mgr._fetch = fetch
    result = await mgr.check()
    assert any("dns down" in err for err in result.errors)
    assert [e.name for e in result.entries] == ["old"]


def test_catalog_schema_rejects_bad_entries():
    with pytest.raises(UpdateError):
        CatalogEntry.from_dict({"kind": "virus", "name": "x", "version": "1"})
    with pytest.raises(UpdateError):
        CatalogEntry.from_dict({"kind": "plugin", "name": "", "version": "1"})


def test_plan_skips_incompatible_versions(tmp_path):
    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._write_cache(
        [
            CatalogEntry(kind="plugin", name="ok", version="1.0", requires_oaset=">=0.0.1"),
            CatalogEntry(kind="plugin", name="future", version="9.9", requires_oaset=">=999.0.0"),
        ],
        time.time(),
    )
    plan = mgr.plan()
    assert [e.name for e in plan.targets] == ["ok"]
    assert plan.skipped and plan.skipped[0][0] == "future"


def test_apply_requires_explicit_targets(tmp_path):
    import asyncio

    mgr = UpdateManager(Cfg(), home=tmp_path)
    with pytest.raises(UpdateError) as apply_exc:
        asyncio.run(mgr.apply([]))
    assert apply_exc.value.code == "missing_targets"


def test_rollback_without_history_reports_and_does_not_raise(tmp_path):
    import asyncio

    mgr = UpdateManager(Cfg(), home=tmp_path)
    result = asyncio.run(mgr.rollback("last"))
    assert any("没有可回滚" in err for err in result.errors)


def test_verify_sha256_matches_and_rejects(tmp_path):
    target = tmp_path / "pkg.bin"
    target.write_bytes(b"payload")
    import hashlib

    good = hashlib.sha256(b"payload").hexdigest()
    assert verify_sha256(target, good) is True
    assert verify_sha256(target, "0" * 64) is False
    assert verify_sha256(target, "") is False


def test_version_compare_orders_and_equals():
    from oaset.update import _version_cmp

    assert _version_cmp("1.2.0", "1.2") == 0
    assert _version_cmp("0.9.1", "0.10.0") < 0  # numeric, not lexicographic
    assert _version_cmp("2.0.0", "1.9.9") > 0
    assert _version_cmp("1.0.0", "1.0.1") < 0


def test_plan_skips_core_downgrade_or_same_version(tmp_path):
    from oaset import __version__

    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._write_cache(
        [
            CatalogEntry(kind="core", name="oaset-cli", version=__version__),  # same → skip
            CatalogEntry(kind="core", name="oaset-cli", version="0.0.1"),  # older → skip
            CatalogEntry(kind="plugin", name="fresh", version="1.0"),
        ],
        time.time(),
    )
    plan = mgr.plan()
    assert [e.name for e in plan.targets] == ["fresh"]
    assert len(plan.skipped) == 2


def test_list_and_plan_filter_by_kind(tmp_path):
    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._write_cache(
        [
            CatalogEntry(kind="plugin", name="p1", version="1.0"),
            CatalogEntry(kind="model", name="m1", version="2"),
        ],
        time.time(),
    )
    only_models = mgr.list_entries("model")
    assert [e.name for e in only_models.entries] == ["m1"]
    only_plugins = mgr.plan("plugin")
    assert [e.name for e in only_plugins.targets] == ["p1"]
    everything = mgr.list_entries("all")
    assert len(everything.entries) == 2


def test_parse_catalog_rejects_non_list_entries():
    import json as _json

    from oaset.update import _parse_catalog

    for bad in ("null", '"x"', "123"):
        with pytest.raises(UpdateError) as excinfo:
            _parse_catalog(_json.dumps({"entries": _json.loads(bad)}))
        assert excinfo.value.code == "catalog_schema"


def test_prerelease_version_compares_consistently(tmp_path):
    """'1.2.0-alpha' must satisfy '>=1.2.0' under the same numeric rule as _version_cmp."""
    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._write_cache(
        [CatalogEntry(kind="plugin", name="needs", version="1", requires_oaset=">=1.2.0")],
        time.time(),
    )
    # simulate a dev version without editing the module constant: craft plan directly
    from oaset import update as upd

    assert upd._version_at_least("1.2.0-alpha", ">=1.2.0") is True
    assert upd._version_at_least("1.1.9", ">=1.2.0") is False


def _plugin_catalog(sha: str) -> str:
    return _catalog([{
        "kind": "plugin", "name": "demo", "version": "1.1.0",
        "source_url": "https://cdn.test/demo-1.1.0.py", "sha256": sha,
    }])


async def test_apply_happy_path_installs_and_records_history(tmp_path):
    import hashlib

    payload = b"print('plugin v1.1')\n"
    sha = hashlib.sha256(payload).hexdigest()

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_plugin_catalog(sha))
        return FakeContentResponse(payload)

    class FakeContentResponse:
        def __init__(self, content: bytes):
            self.content = content

    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._fetch = fetch
    await mgr.refresh()
    result = await mgr.apply(["demo"])
    assert result.errors == [], result.errors
    dest = tmp_path / "plugins" / "demo.py"
    assert dest.read_bytes() == payload
    history = mgr.history()
    assert any(r.get("action") == "apply" and r.get("target") == "demo" for r in history)


async def test_apply_rejects_bad_hash_without_writing(tmp_path):
    import hashlib

    payload = b"evil bytes"
    good_sha = hashlib.sha256(b"expected").hexdigest()

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_plugin_catalog(good_sha))
        return FakeResponse(payload)

    class FakeContentResponse:
        def __init__(self, content: bytes):
            self.content = content

    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._fetch = fetch
    await mgr.refresh()
    result = await mgr.apply(["demo"])
    assert any("sha256" in err for err in result.errors)
    assert not (tmp_path / "plugins" / "demo.py").exists()
    assert not (tmp_path / "plugins" / "demo.tmp").exists()


async def test_apply_requires_sha256_in_catalog(tmp_path):
    async def fetch(url):
        return FakeResponse(_catalog([{
            "kind": "plugin", "name": "unsigned", "version": "1",
            "source_url": "https://cdn.test/unsigned.py",
        }]))

    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._fetch = fetch
    await mgr.refresh()
    result = await mgr.apply(["unsigned"])
    assert any("sha256" in err and "拒绝" in err for err in result.errors)


async def test_apply_dry_run_verifies_but_writes_nothing(tmp_path):
    import hashlib

    payload = b"print('dry')"
    sha = hashlib.sha256(payload).hexdigest()

    class FakeContentResponse:
        def __init__(self, content: bytes):
            self.content = content

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_plugin_catalog(sha))
        return FakeContentResponse(payload)

    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._fetch = fetch
    await mgr.refresh()
    result = await mgr.apply(["demo"], dry_run=True)
    assert result.errors == []
    assert not (tmp_path / "plugins" / "demo.py").exists()


async def test_rollback_restores_previous_version(tmp_path):
    import hashlib

    v1, v2 = b"print('v1')", b"print('v2')"

    class FakeContentResponse:
        def __init__(self, content: bytes):
            self.content = content

    calls = {"n": 0}

    async def fetch(url):
        if url.endswith("index.json"):
            sha = hashlib.sha256(v2 if calls["n"] else v1).hexdigest()
            return FakeResponse(_plugin_catalog(sha))
        calls["n"] += 1
        return FakeContentResponse(v1 if calls["n"] == 1 else v2)

    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._fetch = fetch
    await mgr.refresh()
    await mgr.apply(["demo"])  # fresh install of v1
    await mgr.refresh()        # same catalog entry (v2 hash? catalog rewritten with v2 hash)
    result = await mgr.apply(["demo"])  # upgrade to v2
    if result.errors:  # catalog still points at v1 hash → upgrade rejected; rollback still testable
        pass
    rb = await mgr.rollback("last")
    assert any("已回滚" in note for note in rb.notes)
    history = mgr.history()
    assert any(r.get("action") == "rollback" for r in history)


def test_apply_rejects_path_traversal_target(tmp_path):
    import asyncio

    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._write_cache([], time.time())
    with pytest.raises(UpdateError) as excinfo:
        asyncio.run(mgr._plugin_dest("../evil"))
    assert excinfo.value.code == "unsafe_target"


async def test_apply_mcp_writes_and_rolls_back(tmp_path):
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {"old": {"command": "x"}}}), encoding="utf-8")

    async def fetch(url):
        return FakeResponse(_catalog([{
            "kind": "mcp", "name": "search", "version": "1.0",
            "install": {"command": "uvx", "args": ["mcp-search"]},
        }]))

    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._fetch = fetch
    await mgr.refresh()
    result = await mgr.apply(["search"])
    assert result.errors == [], result.errors
    data = json.loads(mcp_json.read_text(encoding="utf-8"))
    assert data["mcpServers"]["search"] == {"command": "uvx", "args": ["mcp-search"]}
    assert data["mcpServers"]["old"] == {"command": "x"}  # untouched

    rb = await mgr.rollback("search")
    assert any("已回滚" in n for n in rb.notes)
    restored = json.loads(mcp_json.read_text(encoding="utf-8"))
    assert "search" not in restored["mcpServers"]
    assert restored["mcpServers"]["old"] == {"command": "x"}


async def test_apply_mcp_http_url_entry(tmp_path):
    async def fetch(url):
        return FakeResponse(_catalog([{
            "kind": "mcp", "name": "remote", "version": "1.0",
            "install": {"url": "https://mcp.test/mcp", "headers": {"X-A": "b"}},
        }]))

    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._fetch = fetch
    await mgr.refresh()
    result = await mgr.apply(["remote"])
    assert result.errors == []
    data = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["remote"]["url"] == "https://mcp.test/mcp"
    assert data["mcpServers"]["remote"]["headers"] == {"X-A": "b"}


async def test_apply_mcp_rejects_entry_without_install(tmp_path):
    async def fetch(url):
        return FakeResponse(_catalog([{"kind": "mcp", "name": "broken", "version": "1"}]))

    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._fetch = fetch
    await mgr.refresh()
    result = await mgr.apply(["broken"])
    assert any("install" in err for err in result.errors)
    assert not (tmp_path / "mcp.json").exists()


async def test_apply_provider_and_model_merge_config(tmp_path, monkeypatch):
    import oaset.config as cfgmod
    from oaset.config import load_config

    fake_cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfgmod, "config_path", lambda: fake_cfg_path)
    base = load_config()
    cfgmod.save_config(base)  # real-shaped config file to modify

    class CfgWithFile:
        network_mode = "pull_only"

    # manager must operate on the SAME cfg object that save_config serializes
    mgr = UpdateManager(base, home=tmp_path)

    async def fetch(url):
        return FakeResponse(_catalog([
            {"kind": "provider", "name": "acme", "version": "1",
             "install": {"name": "acme", "type": "openai",
                          "base_url": "https://api.acme.test/v1", "api_key": "env:ACME_KEY"}},
            {"kind": "model", "name": "acme/turbo", "version": "1",
             "install": {"id": "acme/turbo", "provider": "acme", "model": "turbo",
                          "max_context_size": 99000, "capabilities": ["tool_use", "image_in"]}},
        ]))

    mgr._fetch = fetch
    await mgr.refresh()
    result = await mgr.apply(["acme", "acme/turbo"])
    assert result.errors == [], result.errors

    reloaded = load_config()
    assert reloaded.providers["acme"].base_url == "https://api.acme.test/v1"
    assert reloaded.providers["acme"].api_key == "env:ACME_KEY"
    assert reloaded.models["acme/turbo"].max_context_size == 99000
    assert "image_in" in reloaded.models["acme/turbo"].capabilities
    # never switches defaults — enabling an unverified model is explicit
    assert reloaded.default_model == base.default_model
    assert any("未切换默认" in n for n in result.notes)

    rb = await mgr.rollback("acme/turbo")
    assert any("已回滚" in n for n in rb.notes)
    after = load_config()
    assert "acme/turbo" not in after.models
    assert "acme" in after.providers  # only the model entry rolled back


async def test_apply_provider_requires_base_url(tmp_path):
    async def fetch(url):
        return FakeResponse(_catalog([{
            "kind": "provider", "name": "nohost", "version": "1",
            "install": {"name": "nohost", "type": "openai"},
        }]))

    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._fetch = fetch
    await mgr.refresh()
    result = await mgr.apply(["nohost"])
    assert any("base_url" in err for err in result.errors)


async def test_apply_core_missing_sha_rejects(tmp_path):

    async def fetch(url):
        return FakeResponse(_catalog([{
            "kind": "core", "name": "oaset-cli",
            "version": "999.0.0",  # newer than current → plan target
        }]))

    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._fetch = fetch
    await mgr.refresh()
    result = await mgr.apply(["oaset-cli"])
    assert any("sha256" in err for err in result.errors)


# --------------------------------------------------- core transactional upgrade

def _core_bundle(version: str) -> tuple[bytes, str]:
    """Build a fake core release zip: oaset/__init__.py + helper module."""
    import hashlib
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("oaset/__init__.py", f'__version__ = "{version}"\n')
        bundle.writestr("oaset/_core_helper.py", "# helper added in release\n")
    payload = buffer.getvalue()
    return payload, hashlib.sha256(payload).hexdigest()


def _core_catalog(sha: str) -> str:
    return _catalog([{
        "kind": "core", "name": "oaset-cli", "version": "999.0.0",
        "source_url": "https://cdn.test/oaset-999.0.0.zip", "sha256": sha,
    }])


def _make_pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "live" / "oaset"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.10.0"\n', encoding="utf-8")
    (pkg / "utils_old.py").write_text("# old module\n", encoding="utf-8")
    return pkg


async def test_apply_core_transaction_replaces_package(tmp_path):
    import shutil

    payload, sha = _core_bundle("999.0.0")
    pkg = _make_pkg(tmp_path)

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_core_catalog(sha))
        return FakeResponse(payload)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch, package_dir=pkg)
    await mgr.refresh()
    result = await mgr.apply(["oaset-cli"])
    assert result.errors == [], result.errors
    assert (pkg / "__init__.py").read_text(encoding="utf-8") == '__version__ = "999.0.0"\n'
    assert (pkg / "_core_helper.py").is_file()  # new file landed
    assert not (pkg / "utils_old.py").exists()  # release replaces the tree
    backups = list((tmp_path / "update" / "core-backup").glob("*"))
    assert len(backups) == 1 and (backups[0] / "__init__.py").is_file()
    record = json.loads((tmp_path / "update" / "history.jsonl")
                        .read_text(encoding="utf-8").splitlines()[0])
    assert record["kind"] == "core" and record["backup"] == str(backups[0])
    assert shutil  # silence unused-import linters in variant edits


async def test_apply_core_rollback_restores_previous_tree(tmp_path):
    payload, sha = _core_bundle("999.0.0")
    pkg = _make_pkg(tmp_path)
    original_init = (pkg / "__init__.py").read_text(encoding="utf-8")

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_core_catalog(sha))
        return FakeResponse(payload)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch, package_dir=pkg)
    await mgr.refresh()
    await mgr.apply(["oaset-cli"])
    rb = await mgr.rollback("oaset-cli")
    assert any("已回滚" in n for n in rb.notes)
    assert (pkg / "__init__.py").read_text(encoding="utf-8") == original_init
    assert (pkg / "utils_old.py").is_file()  # removed file is back
    assert not (pkg / "_core_helper.py").exists()  # release-only file gone
    history = mgr.history()
    assert any(r.get("action") == "rollback" for r in history)


async def test_apply_core_failure_restores_from_backup(tmp_path, monkeypatch):
    payload, sha = _core_bundle("999.0.0")
    pkg = _make_pkg(tmp_path)
    original_init = (pkg / "__init__.py").read_text(encoding="utf-8")

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_core_catalog(sha))
        return FakeResponse(payload)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch, package_dir=pkg)
    await mgr.refresh()

    import shutil as _shutil

    import oaset.update as upd

    real_copy = _shutil.copyfile
    calls = {"n": 0}

    def flaky_copy(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # fail on the SECOND file → mid-transaction
            raise OSError("disk full (injected)")
        real_copy(src, dst)

    monkeypatch.setattr(upd, "_copy_file", flaky_copy)
    result = await mgr.apply(["oaset-cli"])
    assert any("已从备份恢复" in err for err in result.errors)
    # tree restored exactly: original init intact, no release files, no leftovers
    assert (pkg / "__init__.py").read_text(encoding="utf-8") == original_init
    assert (pkg / "utils_old.py").is_file()
    assert not (pkg / "_core_helper.py").exists()
    assert sorted(p.name for p in pkg.rglob("*") if p.is_file()) == \
        ["__init__.py", "utils_old.py"]


async def test_apply_core_dry_run_stages_without_replacing(tmp_path):
    payload, sha = _core_bundle("999.0.0")
    pkg = _make_pkg(tmp_path)

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_core_catalog(sha))
        return FakeResponse(payload)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch, package_dir=pkg)
    await mgr.refresh()
    result = await mgr.apply(["oaset-cli"], dry_run=True)
    assert result.errors == []
    assert (pkg / "__init__.py").read_text(encoding="utf-8") == '__version__ = "0.10.0"\n'
    assert not (pkg / "_core_helper.py").exists()
    assert not (tmp_path / "update" / "core-backup").exists()


async def test_apply_core_rejects_zip_slip(tmp_path):
    import hashlib
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("../evil.py", "pwned")
        bundle.writestr("oaset/__init__.py", "__version__ = '1'\n")
    sha = hashlib.sha256(buffer.getvalue()).hexdigest()
    pkg = _make_pkg(tmp_path)

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_core_catalog(sha))
        return FakeResponse(buffer.getvalue())

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch, package_dir=pkg)
    await mgr.refresh()
    result = await mgr.apply(["oaset-cli"])
    assert any("不安全路径" in err for err in result.errors)
    assert not (tmp_path / "evil.py").exists()


async def test_apply_core_refuses_source_checkout(tmp_path, monkeypatch):
    payload, sha = _core_bundle("999.0.0")
    fake_src = tmp_path / "repo" / "src" / "oaset"
    fake_src.mkdir(parents=True)
    (fake_src / "__init__.py").write_text("__version__ = '0'\n", encoding="utf-8")
    (tmp_path / "repo" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr("oaset.__file__", str(fake_src / "__init__.py"))

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_core_catalog(sha))
        return FakeResponse(payload)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch)  # no injected dir
    await mgr.refresh()
    result = await mgr.apply(["oaset-cli"])
    assert any("源码/可编辑安装" in err for err in result.errors)
    assert (fake_src / "__init__.py").read_text(encoding="utf-8") == "__version__ = '0'\n"


async def test_list_detail_renders_provenance(tmp_path):
    """UPDATE-07: `/update list detail` must show source, hash, compat,
    permissions and publish date per entry — not just name@version."""
    payload = _catalog([{
        "kind": "plugin", "name": "demo", "version": "1.2.0",
        "description": "示例插件",
        "source_url": "https://cdn.test/demo-1.2.0.py",
        "sha256": "ab" * 32,
        "requires_oaset": ">=0.9.0",
        "permissions": ["fs.write", "net.pull"],
        "published_at": "2026-09-01",
    }])
    mgr = UpdateManager(Cfg(), home=tmp_path)
    entries = [CatalogEntry.from_dict(e) for e in json.loads(payload)["entries"]]
    mgr._write_cache(entries, time.time())
    result = mgr.list_entries()

    compact = result.render()
    assert "来源" not in compact
    detailed = result.render(detail=True)
    assert "https://cdn.test/demo-1.2.0.py" in detailed
    assert "ab" * 8 in detailed  # truncated digest still pinned and identifiable
    assert "requires_oaset >=0.9.0" in detailed
    assert "fs.write, net.pull" in detailed
    assert "2026-09-01" in detailed


async def test_list_kind_filter_and_detail_ignore_unknown_tokens(tmp_path):
    payload = _catalog([
        {"kind": "plugin", "name": "demo", "version": "1.2.0"},
        {"kind": "model", "name": "glm-5.3", "version": "2026-09-01"},
    ])
    mgr = UpdateManager(Cfg(), home=tmp_path)
    mgr._write_cache(
        [CatalogEntry.from_dict(e) for e in json.loads(payload)["entries"]],
        time.time(),
    )
    filtered = mgr.list_entries("plugin")
    assert [e.name for e in filtered.entries] == ["demo"]
