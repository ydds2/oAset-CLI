"""The offline mock is not an HTTP endpoint (regression for P0 finding B2).

It used to be declared as openai + http://127.0.0.1:8399/v1. build_provider
then constructed a real OpenAI-compatible client, so offline:

- switching to mock/mock-echo or verifying it failed with Connection error
  ("model.verify_failed"), and
- the model list showed a localhost URL that users tried to open in a browser.

These tests pin the fix: name/scheme recognition, legacy URL migration, a
MockProvider (never an HTTP client) from build_provider, no probe request, and
a list that no longer prints the bogus URL.
"""

from __future__ import annotations

from oaset.config import (
    MOCK_BASE_URL,
    build_provider,
    default_config,
    is_mock_provider,
    load_config,
    resolve_model,
    save_config,
)
from oaset.providers.mock import MockProvider


class _P:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url


def test_is_mock_provider_recognises_name_scheme_and_legacy_url() -> None:
    assert is_mock_provider("mock")
    assert is_mock_provider("Mock")
    assert is_mock_provider("anything", _P(MOCK_BASE_URL))
    assert is_mock_provider("anything", _P("http://127.0.0.1:8399/v1"))
    assert is_mock_provider("anything", _P("http://127.0.0.1:8399"))
    assert not is_mock_provider("glm", _P("https://open.bigmodel.cn/api/anthropic"))
    assert not is_mock_provider("openai", _P("http://127.0.0.1:8000/v1"))  # local vLLM is real


def test_default_config_mock_has_no_fake_http_url() -> None:
    cfg = default_config()
    assert cfg.providers["mock"].base_url == MOCK_BASE_URL
    assert "8399" not in cfg.providers["mock"].base_url


def test_build_provider_returns_mock_without_network() -> None:
    cfg = default_config()
    provider = build_provider(cfg, resolve_model(cfg, "mock/mock-echo"))
    assert isinstance(provider, MockProvider), "mock must never build an HTTP client"
    assert provider.model_id == "mock/mock-echo"


def test_legacy_mock_url_is_migrated_on_load() -> None:
    cfg = default_config()
    cfg.providers["mock"].base_url = "http://127.0.0.1:8399/v1"  # pre-fix config
    save_config(cfg)

    reloaded = load_config()
    assert reloaded.providers["mock"].base_url == MOCK_BASE_URL
    # and it still builds the offline provider after migration
    assert isinstance(
        build_provider(reloaded, resolve_model(reloaded, "mock/mock-echo")),
        MockProvider,
    )


async def test_switch_to_mock_succeeds_offline_without_probe_request(workspace):
    """The whole /model → mock path must work with no network at all."""
    from oaset.tui.app import OasetApp

    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider(), model_id="mock/mock-echo")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        await app._switch_model_verified("mock/mock-echo")
        await pilot.pause(0.1)
        assert isinstance(app.provider, MockProvider)
        assert app.cfg.default_model == "mock/mock-echo"
        # the probe short-circuits: no stream request was issued to the mock
        assert app.provider.requests == [], "probe must not hit the provider"


async def test_models_list_shows_builtin_label_not_a_url(workspace):
    from oaset.tui.app import OasetApp

    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider(), model_id="mock/mock-echo")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app._models_list()
        await pilot.pause(0.1)
        cards = list(app.chat.query("Static.chat-card"))
        assert cards, "models list card must render"
        text = "\n".join(str(card.content) for card in cards)
        assert "8399" not in text, "the fake localhost URL must not be shown"
        assert "mock" in text


async def test_models_verify_mock_ok_even_under_local_only(capsys):
    """The offline mock probe must succeed with networking fully disabled."""
    import json

    from oaset.cli import cmd_models_verify
    from oaset.config import default_config

    cfg = default_config()
    cfg.network_mode = "local_only"  # strictest policy: no outbound at all
    rc = await cmd_models_verify(cfg, "mock/mock-echo", json_out=True)
    assert rc == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["ok"] is True
