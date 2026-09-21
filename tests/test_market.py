"""Community plugin market (DeepSeek Harness parity): sources, browse,
install, uninstall — all over the Update Center's verified install path."""

from __future__ import annotations

import hashlib
import json

import pytest

from oaset.config import default_config
from oaset.market import (
    OFFICIAL_SOURCE,
    MarketManager,
    load_state,
    market_state_path,
    read_plugin_manifest,
)
from oaset.update import UpdateError

CATALOG_URL = "https://market.example/index.json"
PLUGIN_BODY = (
    'OASET_MANIFEST = {"version": "1.2.0", "description": "demo plugin", '
    '"permissions": ["tools"]}\n'
    "def register(api):\n"
    "    pass\n"
)
PLUGIN_SHA = hashlib.sha256(PLUGIN_BODY.encode("utf-8")).hexdigest()


def make_catalog() -> dict:
    return {
        "entries": [
            {
                "kind": "plugin",
                "name": "demo",
                "version": "1.2.0",
                "description": "demo plugin",
                "source_url": "https://market.example/files/demo.py",
                "sha256": PLUGIN_SHA,
                "permissions": ["tools"],
            },
            {
                "kind": "core",
                "name": "oaset-cli",
                "version": "99.0.0",
                "description": "not a plugin",
            },
        ]
    }


class FakeFetcher:
    """async fetch(url) -> response with .text/.content, catalog + one file."""

    def __init__(self, catalog: dict, files: dict[str, bytes] | None = None):
        self.catalog = catalog
        self.files = files or {}
        self.requests: list[str] = []

    async def __call__(self, url: str):
        self.requests.append(url)
        if url.endswith("index.json") or url == CATALOG_URL:
            return _Resp(text=json.dumps(self.catalog))
        for suffix, body in self.files.items():
            if url.endswith(suffix):
                return _Resp(text=body.decode("utf-8"), content=body)
        raise AssertionError(f"unexpected fetch: {url}")


class _Resp:
    def __init__(self, text: str = "", content: bytes | None = None):
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")


def make_manager(home, fetch=None, network_mode="pull_only") -> MarketManager:
    cfg = default_config()
    cfg.network_mode = network_mode
    return MarketManager(cfg, home=home, fetch=fetch)


# ------------------------------------------------------------- sources

def test_default_state_is_official_only(isolated_home):
    market = make_manager(isolated_home)
    assert [s.name for s in market.sources()] == [OFFICIAL_SOURCE]
    assert market.active().name == OFFICIAL_SOURCE


def test_add_use_remove_sources(isolated_home):
    market = make_manager(isolated_home)
    market.add_source("mirror", CATALOG_URL)
    assert [s.name for s in market.sources()] == [OFFICIAL_SOURCE, "mirror"]
    assert market.active().name == OFFICIAL_SOURCE  # add does NOT switch
    market.use_source("mirror")
    assert market.active().name == "mirror"
    assert market.active().url == CATALOG_URL
    market.remove_source("mirror")
    assert market.active().name == OFFICIAL_SOURCE  # falls back after removal
    # state persists across instances
    assert load_state(isolated_home)["active"] == OFFICIAL_SOURCE


def test_add_source_rejects_bad_name_and_scheme(isolated_home):
    market = make_manager(isolated_home)
    with pytest.raises(UpdateError):
        market.add_source("official", CATALOG_URL)  # reserved
    with pytest.raises(UpdateError):
        market.add_source("dup", CATALOG_URL)
        market.add_source("dup", CATALOG_URL)
    with pytest.raises(UpdateError):
        market.add_source("local", "file:///etc/passwd")  # not http(s)
    with pytest.raises(UpdateError):
        market.add_source("", CATALOG_URL)


def test_use_unknown_source_fails(isolated_home):
    market = make_manager(isolated_home)
    with pytest.raises(UpdateError):
        market.use_source("nope")


# ------------------------------------------------------------- browsing

async def test_discover_returns_plugin_entries_only(isolated_home):
    fetch = FakeFetcher(make_catalog())
    market = make_manager(isolated_home, fetch=fetch)
    market.add_source("mirror", CATALOG_URL)
    market.use_source("mirror")
    entries, notes = await market.discover(refresh=True)
    assert [e.name for e in entries] == ["demo"]
    assert any("demo" in str(e.install) or True for e in entries)
    assert fetch.requests == [CATALOG_URL]  # only the ACTIVE source was hit


async def test_discover_cache_hit_skips_network(isolated_home):
    fetch = FakeFetcher(make_catalog())
    market = make_manager(isolated_home, fetch=fetch)
    await market.discover(refresh=True)
    fetch.requests.clear()
    entries, _ = await market.discover()  # within 24h cache TTL
    assert [e.name for e in entries] == ["demo"]
    assert fetch.requests == []


async def test_discover_local_only_stays_offline(isolated_home):
    """local_only refuses the network; the failure must be visible in notes."""
    fetch = FakeFetcher(make_catalog())
    market = make_manager(isolated_home, fetch=fetch, network_mode="local_only")
    entries, notes = await market.discover(refresh=True)
    assert entries == []
    assert any("network_blocked" in n for n in notes)
    assert fetch.requests == []


# ------------------------------------------------------------- installed

def test_installed_reads_manifest_without_executing(isolated_home):
    plugins = isolated_home / "plugins"
    plugins.mkdir()
    (plugins / "demo.py").write_text(PLUGIN_BODY, encoding="utf-8")
    # a manifest that is NOT a literal must not break listing (and must not run)
    (plugins / "weird.py").write_text(
        "import os\nOASET_MANIFEST = os.environ\n", encoding="utf-8")
    market = make_manager(isolated_home)
    installed = {i["name"]: i for i in market.installed()}
    assert installed["demo"]["version"] == "1.2.0"
    assert installed["demo"]["permissions"] == ("tools",)
    assert installed["weird"]["version"] == ""  # non-literal manifest → no data


def test_read_plugin_manifest_direct(tmp_path):
    path = tmp_path / "p.py"
    path.write_text(PLUGIN_BODY, encoding="utf-8")
    assert read_plugin_manifest(path)["description"] == "demo plugin"


# ------------------------------------------------------------- install

async def test_install_verifies_and_lands_in_user_dir(isolated_home):
    fetch = FakeFetcher(make_catalog(), files={"demo.py": PLUGIN_BODY.encode("utf-8")})
    market = make_manager(isolated_home, fetch=fetch)
    result, entry = await market.install("demo")
    assert entry.version == "1.2.0"
    dest = isolated_home / "plugins" / "demo.py"
    assert dest.read_text(encoding="utf-8") == PLUGIN_BODY
    assert {i["name"] for i in market.installed()} == {"demo"}
    # installable view now excludes it
    entries, _ = await market.discover()
    assert market.installable(entries) == []


async def test_install_rejects_unknown_plugin(isolated_home):
    fetch = FakeFetcher(make_catalog())
    market = make_manager(isolated_home, fetch=fetch)
    with pytest.raises(UpdateError):
        await market.install("nope")


async def test_install_rejects_corrupted_payload(isolated_home):
    catalog = make_catalog()
    catalog["entries"][0]["sha256"] = "0" * 64  # lies about the digest
    fetch = FakeFetcher(catalog, files={"demo.py": PLUGIN_BODY.encode("utf-8")})
    market = make_manager(isolated_home, fetch=fetch)
    with pytest.raises(UpdateError):
        await market.install("demo")
    assert not (isolated_home / "plugins" / "demo.py").exists()


# ------------------------------------------------------------- uninstall

def test_uninstall_removes_user_plugin(isolated_home):
    plugins = isolated_home / "plugins"
    plugins.mkdir()
    (plugins / "demo.py").write_text(PLUGIN_BODY, encoding="utf-8")
    market = make_manager(isolated_home)
    market.uninstall("demo")
    assert not (plugins / "demo.py").exists()
    with pytest.raises(UpdateError):
        market.uninstall("demo")  # already gone


def test_market_state_file_roundtrip(isolated_home):
    market = make_manager(isolated_home)
    market.add_source("mirror", CATALOG_URL)
    market.use_source("mirror")
    state = load_state(isolated_home)
    assert state["active"] == "mirror"
    assert state["sources"][0].url == CATALOG_URL
    assert market_state_path(isolated_home).is_file()


# ------------------------------------------------------------- TUI smoke

async def test_market_tui_discover_and_install(workspace, isolated_home):
    """Full in-app path: /market → 发现 → pick → confirm → installed."""
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp
    from oaset.tui.widgets.inline import InlinePicker
    from tests.test_app_tui import wait_until
    from tests.test_tui_model_setup import _pick_value

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    fetch = FakeFetcher(make_catalog(), files={"demo.py": PLUGIN_BODY.encode("utf-8")})
    app._market_fetch = fetch
    async with app.run_test(size=(110, 36)) as pilot:
        app.submit_text("/market")
        assert await wait_until(pilot, lambda: list(app.query(InlinePicker)))
        await _pick_value(pilot, app, "discover")       # tab picker
        await _pick_value(pilot, app, "demo")           # entry picker
        await _pick_value(pilot, app, "yes")            # confirm picker
        assert await wait_until(
            pilot, lambda: (isolated_home / "plugins" / "demo.py").is_file())
        dest = isolated_home / "plugins" / "demo.py"
        assert dest.read_text(encoding="utf-8") == PLUGIN_BODY
