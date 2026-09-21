
import pytest

from oaset.config import (
    MOCK_BASE_URL,
    config_path,
    load_config,
    resolve_api_key,
    resolve_model,
    save_config,
)


def test_load_creates_default_file():
    cfg = load_config()
    assert config_path().exists()
    assert "mock/mock-echo" in cfg.models
    # the offline mock is not an HTTP endpoint (see tests/test_mock_offline.py)
    assert cfg.providers["mock"].base_url == MOCK_BASE_URL


def test_save_load_roundtrip(cfg):
    cfg.default_model = "openai/gpt-4o"
    cfg.tool_output_limit = 500
    cfg.ui.theme = "oaset-light"
    save_config(cfg)
    loaded = load_config()
    assert loaded.default_model == "openai/gpt-4o"
    assert loaded.tool_output_limit == 500
    assert loaded.ui.theme == "oaset-light"
    assert loaded.models["moonshot/kimi-k2"].reasoning_key == "reasoning_content"
    assert loaded.models["moonshot/kimi-k2"].has("thinking")


def test_resolve_api_key_env(monkeypatch):
    monkeypatch.setenv("MY_SECRET_KEY", "abc123")
    assert resolve_api_key("env:MY_SECRET_KEY") == "abc123"
    assert resolve_api_key("plain-key") == "plain-key"
    assert resolve_api_key("env:MISSING_VAR") == ""


def test_resolve_model_unknown_lists_known(cfg):
    with pytest.raises(KeyError) as exc:
        resolve_model(cfg, "nope/nope")
    assert "mock/mock-echo" in str(exc.value)


def test_default_config_wiring(cfg):
    model = resolve_model(cfg, None)
    assert model.provider in cfg.providers
