"""“在中国也能用” claims must be true in code, not just in the README.

1. GLM / DeepSeek / Kimi are first-class: in the DEFAULT config, with
   mainland-reachable endpoints, before any user edits.
2. Web search works with zero configuration through Bing RSS (no API key,
   no SearXNG, endpoint reachable from mainland China).
3. Mirror-friendly: the update catalog URL is overridable so `oaset update`
   never hard-depends on GitHub Pages.
"""

from __future__ import annotations

import pytest

from oaset.config import default_config, load_config, save_config


def test_domestic_providers_are_first_class_by_default():
    cfg = default_config()
    for name in ("deepseek", "moonshot", "glm"):
        assert name in cfg.providers, f"{name} must ship in the default table"
        assert cfg.providers[name].base_url, f"{name} needs an endpoint"
    # endpoints resolve inside mainland China (no proxy in the path)
    assert cfg.providers["deepseek"].base_url.startswith("https://api.deepseek.com")
    assert cfg.providers["moonshot"].base_url.startswith("https://api.moonshot.cn")
    assert cfg.providers["glm"].base_url.startswith("https://open.bigmodel.cn")
    # and one of them is the out-of-the-box default
    assert cfg.default_provider == "deepseek"


def test_bing_rss_is_zero_config():
    from oaset.tools.search import default_engines

    engines = default_engines()  # no config, no keys, no SearXNG URL
    assert "bing_rss" in engines, "the zero-config fallback must always be present"
    assert engines[0] == "bing_rss", "bing_rss leads when nothing is configured"


def test_catalog_mirror_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("OASET_HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.update_catalog_url == ""
    cfg.update_catalog_url = "https://mirror.example.net/oaset/catalog.json"
    save_config(cfg)
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "catalog_url" in text
    assert load_config().update_catalog_url == "https://mirror.example.net/oaset/catalog.json"


def test_update_manager_honors_mirror():
    from oaset.update import UpdateManager

    cfg = default_config()
    mgr = UpdateManager(cfg)
    assert mgr.source.name == "official"

    cfg.update_catalog_url = "https://mirror.example.net/oaset/catalog.json"
    mgr = UpdateManager(cfg)
    assert mgr.source.name == "mirror"
    assert mgr.source.url == cfg.update_catalog_url
    assert mgr.source.trusted, "a user-set mirror is as trusted as the official one"


def test_update_manager_rejects_non_http_mirror():
    from oaset.update import UpdateError, UpdateManager

    cfg = default_config()
    cfg.update_catalog_url = "file:///etc/passwd"
    with pytest.raises(UpdateError):
        UpdateManager(cfg)  # refused at construction, before any network I/O
