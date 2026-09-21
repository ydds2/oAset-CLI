"""Credential store + provider fallback + TUI/CLI login behavior."""

from __future__ import annotations

from oaset.config import build_provider, default_config
from oaset.credentials import (
    delete_credential,
    list_credentials,
    load_credential,
    save_credential,
)


def test_credential_roundtrip(isolated_home):
    assert list_credentials() == []
    save_credential("deepseek", "sk-secret-123")
    assert load_credential("deepseek") == "sk-secret-123"
    assert list_credentials() == ["deepseek"]
    save_credential("deepseek", "sk-rotated")
    assert load_credential("deepseek") == "sk-rotated"
    assert delete_credential("deepseek") is True
    assert load_credential("deepseek") == ""
    assert delete_credential("deepseek") is True  # idempotent


def test_provider_falls_back_to_credential(isolated_home):
    cfg = default_config()
    cfg.providers["deepseek"].api_key = ""  # nothing in config/env
    provider = build_provider(cfg, cfg.models["deepseek/deepseek-chat"])
    assert provider._client.api_key == "EMPTY"  # no credential yet
    save_credential("deepseek", "sk-from-login")
    provider = build_provider(cfg, cfg.models["deepseek/deepseek-chat"])
    assert provider._client.api_key == "sk-from-login"


def test_config_key_wins_over_credential(isolated_home, monkeypatch):
    save_credential("deepseek", "sk-from-login")
    cfg = default_config()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    provider = build_provider(cfg, cfg.models["deepseek/deepseek-chat"])
    assert provider._client.api_key == "sk-from-env"  # env: reference has priority


def test_custom_home_parameter(isolated_home, tmp_path):
    other = tmp_path / "elsewhere"
    save_credential("openai", "sk-x", home=other)
    assert load_credential("openai", home=other) == "sk-x"
    assert load_credential("openai") == ""  # default home untouched
