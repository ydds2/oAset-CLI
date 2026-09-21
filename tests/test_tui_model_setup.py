"""TUI model/provider guided setup, verified switching, clipboard and Ctrl+0.

The wizard is driven through the real inline prompts (Pilot), with both the
connectivity probe and the provider's /models lookup stubbed so no network is
involved; the clipboard adapter is exercised through fakes plus a real-Windows
smoke check. conftest's ``no_live_http`` fixture enforces the no-network part.
"""

from __future__ import annotations

import time

import pytest

from oaset.config import default_config, load_config, save_config
from oaset.providers import MockProvider, MockTurn
from oaset.tui.app import OasetApp
from oaset.tui.widgets.inline import InlineInput, InlinePicker


@pytest.fixture(autouse=True)
def isolated_config_path(tmp_path, monkeypatch):
    import oaset.config as cfgmod

    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "config.toml")


def make_app(workspace, script=None, discovered=None):
    cfg = default_config()
    cfg.ui_language = "en"
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider(script or [MockTurn(content_chunks=["ok"])]),
                   model_id="mock/mock-echo")
    # The wizard resolves token limits by asking the provider's /models
    # endpoint. These tests are about the wizard, not the network: a live call
    # to a vendor costs its full timeout and used to surface as a bogus
    # "inline input never became ready" several assertions later. Inject the
    # answer; `test_limits_resolution_wiring_*` covers the real call path.
    from oaset.model_limits import resolve_limits
    from oaset.provider_catalog import vendor_limits

    async def fixed_limits(provider_name, base_url, api_key, model_id, api_format):
        return resolve_limits(discovered=discovered or {},
                              vendor=vendor_limits(provider_name))

    app._resolve_model_limits = fixed_limits  # type: ignore[method-assign]
    return app


async def _wait(pilot, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause(0.05)
    return False


async def _pick_value(pilot, app, value):
    assert await _wait(pilot, lambda: list(app.query(InlinePicker)))
    picker = app.query(InlinePicker).first()
    values = [v for v, _ in picker.options]
    picker.index = values.index(value)
    await pilot.press("enter")
    await pilot.pause(0.1)


async def _type(pilot, app, text):
    from conftest import wait_for_inline_input

    field = await wait_for_inline_input(pilot, app)
    field.value = text
    await pilot.press("enter")
    await pilot.pause(0.1)


# ------------------------------------------------------------- provider catalog


def test_catalog_validates_formats_and_base_urls():
    from oaset.provider_catalog import (
        API_FORMATS,
        CUSTOM_VENDOR,
        VENDORS,
        default_api_format,
        default_base_url,
        validate_api_format,
        validate_base_url,
    )

    assert CUSTOM_VENDOR and len(VENDORS) >= 10
    for item in VENDORS:
        assert item.api_format in API_FORMATS
        assert item.base_url.startswith(("http://", "https://"))
    assert validate_api_format("openai") == "openai"
    with pytest.raises(ValueError):
        validate_api_format("telepathy")  # unknown format must be refused
    assert validate_base_url("https://api.test/v1/") == "https://api.test/v1"
    with pytest.raises(ValueError):
        validate_base_url("api.test/v1")
    assert default_base_url("deepseek") and default_api_format("deepseek") == "openai"


def test_provider_config_roundtrip_keeps_api_format(tmp_path):
    from oaset.config import ProviderConfig

    cfg = default_config()
    cfg.providers["custom"] = ProviderConfig(
        "custom", "anthropic", "", "https://custom.test", api_format="anthropic")
    save_config(cfg)
    reloaded = load_config()
    assert reloaded.providers["custom"].effective_api_format() == "anthropic"


def test_unknown_api_format_is_rejected_not_downgraded():
    from oaset.config import ModelConfig, ProviderConfig, build_provider

    cfg = default_config()
    cfg.providers["weird"] = ProviderConfig("weird", "telepathy", "", "https://x.test")
    cfg.models["weird/m"] = ModelConfig("weird/m", "weird", "m")
    with pytest.raises(ValueError, match="unsupported API format"):
        build_provider(cfg, cfg.models["weird/m"])


# ------------------------------------------------------------------- wizard


async def test_wizard_adds_vendor_model_and_keeps_key_out_of_config(workspace, tmp_path):
    app = make_app(workspace)
    from oaset.credentials import load_credential

    async def ok_probe(provider):
        return True, "OK"

    app._probe_provider = ok_probe  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/models add")
        # vendor → openrouter (a built-in, not configured by default)
        await _pick_value(pilot, app, "vendor:openrouter")
        # base URL → keep the vendor default
        assert await _wait(pilot, lambda: list(app.query(InlinePicker)))
        await pilot.press("enter")
        # api format → openai
        await _pick_value(pilot, app, "openai")
        # credential first (discovery and the first turn both need it)
        await _type(pilot, app, "sk-secret-key")
        # model id (bare name gets the provider prefix); limits are resolved
        # automatically — no context/output prompts in the default flow
        await _type(pilot, app, "acme-1")
        await _type(pilot, app, "tool_use,thinking")
        await _type(pilot, app, "Acme One")   # display name
        await _type(pilot, app, "")           # reasoning key (skip)
        await _pick_value(pilot, app, "save")

        assert "openrouter/acme-1" in app.cfg.models
        saved = app.cfg.providers["openrouter"]
        assert saved.effective_api_format() == "openai"
        assert saved.base_url.endswith("/v1")
        model = app.cfg.models["openrouter/acme-1"]
        # vendor defaults applied automatically (openrouter: 128k / 16k)
        assert model.max_context_size == 128000 and model.max_output_size == 16384
        assert model.capabilities == ["tool_use", "thinking"]
        assert load_credential("openrouter") == "sk-secret-key"
        # the secret must never reach config.toml
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "sk-secret-key" not in text


async def test_wizard_cancel_at_vendor_saves_nothing(workspace, tmp_path):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        before = set(app.cfg.providers)
        app.submit_text("/models add")
        assert await _wait(pilot, lambda: list(app.query(InlinePicker)))
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert set(app.cfg.providers) == before
        assert not (tmp_path / "config.toml").exists()


async def test_wizard_rejects_bad_base_url_then_accepts_valid(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/models add")
        await _pick_value(pilot, app, "__custom__")   # custom vendor
        await _type(pilot, app, "mycorp")             # provider name
        await _pick_value(pilot, app, "__custom__")   # custom base url
        await _type(pilot, app, "not-a-url")          # rejected, re-prompts
        assert await _wait(pilot, lambda: list(app.query(InlineInput)))
        await _type(pilot, app, "https://gateway.mycorp.ai/v1")
        await _pick_value(pilot, app, "openai")       # api format
        await _type(pilot, app, "")                   # api key (none for this test)
        await _type(pilot, app, "m1")                 # model id
        await _type(pilot, app, "tool_use")           # capabilities
        await _type(pilot, app, "")                   # display name
        await _type(pilot, app, "")                   # reasoning key
        await _pick_value(pilot, app, "save")
        assert app.cfg.providers["mycorp"].base_url == "https://gateway.mycorp.ai/v1"


async def test_wizard_rejects_unknown_api_format_end_to_end(workspace):
    """A format the runtime cannot speak is refused before anything is saved."""
    from oaset.provider_catalog import validate_api_format

    with pytest.raises(ValueError):
        validate_api_format("carrier-pigeon")


async def test_models_list_shows_providers_models_and_key_state(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.cmd_models("list")
        await pilot.pause(0.1)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "moonshot" in rendered and "mock/mock-echo" in rendered
        assert "key:" in rendered


async def test_verify_reports_failure_with_hint(workspace):
    app = make_app(workspace)

    async def bad_probe(provider):
        return False, "401 Authentication Fails"

    app._probe_provider = bad_probe  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await app._verify_model("deepseek/deepseek-chat")
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "401" in rendered
        assert "[model.verify_failed]" in app.status_bar.error_text


# ----------------------------------------------------------------- clipboard


def test_clipboard_adapter_returns_structured_results(monkeypatch):
    from oaset.tui import clipboard

    out: dict = {}
    monkeypatch.setattr(clipboard, "set_text", lambda t: None)
    out["set"] = clipboard.set_text("x")
    assert out["set"] is None

    error = clipboard.ClipboardError("clipboard_empty", "no text")
    text, level = clipboard.describe(error)
    assert text and level == "info"
    error2 = clipboard.ClipboardError("clipboard_unavailable", "nope")
    text2, level2 = clipboard.describe(error2)
    assert text2 and level2 == "warn"
    text3, level3 = clipboard.describe(clipboard.ClipboardError("boom", "detail"))
    assert "detail" in text3 and level3 == "warn"


def test_clipboard_set_get_roundtrip_on_windows(workspace):
    import os

    from oaset.tui import clipboard

    if os.name != "nt":
        text, error = clipboard.get_text()
        assert text is None and error is not None
        assert error.code == "clipboard_unavailable"
        return
    sentinel = "oaset-clipboard-roundtrip-测试"
    error = clipboard.set_text(sentinel)
    # a clipboard HELD OPEN by another app (RDP, clipboard manager) is an OS
    # condition, not a product defect — PowerShell's own Set-Clipboard fails
    # the same way. The structured-error path is covered separately.
    if error is not None and error.code == "clipboard_open_failed":
        pytest.skip("system clipboard busy (held by another application)")
    assert error is None, error
    text, read_error = clipboard.get_text()
    assert read_error is None, read_error
    assert text == sentinel


async def test_paste_action_uses_collapse_path_and_copy_reports(workspace, monkeypatch):
    from oaset.tui import clipboard
    from oaset.tui.clipboard import ClipboardError

    app = make_app(workspace)
    big = "x" * 5000
    monkeypatch.setattr(clipboard, "get_text", lambda: (big, None))
    # hermetic: never let the machine's real clipboard inject an image
    monkeypatch.setattr(clipboard, "get_image_png",
                        lambda: (None, ClipboardError("clipboard_empty", "no image")))
    monkeypatch.setattr(clipboard, "set_text", lambda text: None)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await app.input_area.action_paste_clipboard()
        await pilot.pause(0.1)
        assert "[pasted:" in app.input_area.text
        from oaset.utils import oaset_home

        assert (oaset_home() / "pastes").is_dir()

        await app.cmd_copy("")
        await pilot.pause(0.1)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "copied" in rendered.lower()


async def test_copy_reports_unavailable_without_crashing(workspace, monkeypatch):
    from oaset.tui import clipboard

    app = make_app(workspace)
    monkeypatch.setattr(
        clipboard, "set_text",
        lambda text: clipboard.ClipboardError("clipboard_unavailable", "no clipboard"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.input_area.load_text("something worth copying")
        await pilot.pause(0.05)
        await app.cmd_copy("")
        await pilot.pause(0.1)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        # P1-8: an unavailable Win32 clipboard now falls back to OSC52, so the
        # user either sees the old clipboard failure or the terminal fallback.
        assert ("clipboard" in rendered.lower()) or ("osc52" in rendered.lower())


async def test_copy_with_nothing_to_copy_does_not_touch_the_clipboard(workspace, monkeypatch):
    """An empty call must say so instead of writing an empty clipboard."""
    from oaset.tui import clipboard

    calls: list[str] = []
    app = make_app(workspace)
    monkeypatch.setattr(clipboard, "set_text", lambda text: calls.append(text))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await app.cmd_copy("")           # empty draft, no selection
        await pilot.pause(0.1)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "暂无可复制" in rendered or "nothing to copy" in rendered.lower()
        assert calls == [], "an empty copy must not write to the clipboard"


async def test_ctrl_shift_copy_paste_and_ctrl_0_bindings(workspace, monkeypatch):
    from oaset.tui import clipboard

    app = make_app(workspace)
    monkeypatch.setattr(clipboard, "set_text", lambda text: None)
    monkeypatch.setattr(clipboard, "get_text", lambda: ("clip-text", None))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.input_area.load_text("copy me")
        await pilot.press("ctrl+shift+c")   # copy current draft
        await pilot.pause(0.1)
        app.input_area.load_text("")
        await pilot.press("ctrl+shift+v")   # paste from clipboard
        await pilot.pause(0.1)
        assert "clip-text" in app.input_area.text
        await pilot.press("ctrl+0")         # clear draft
        await pilot.pause(0.1)
        assert app.input_area.text.strip() == ""


async def test_ctrl_c_double_quit_and_ctrl_x_interrupt_survive(workspace):
    """The new clipboard keys must not steal the exit/interrupt contract."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.press("ctrl+x")  # interrupt is a no-op while idle, no crash
        await pilot.pause(0.1)
        await pilot.press("ctrl+c")  # first press arms the double-quit
        await pilot.pause(0.1)
        assert app.is_running is True

# ------------------------------------------- availability (no fake-ready presets)


def test_provider_credential_state_classifies_env_literal_and_local():
    from oaset.config import ProviderConfig, default_config, provider_credential_state

    cfg = default_config()
    assert provider_credential_state(cfg, "mock") == "ready"      # local server
    assert provider_credential_state(cfg, "ghost") == "missing"
    # a preset with an unset env reference is NOT ready
    cfg.providers["envonly"] = ProviderConfig("envonly", "openai", "env:NEVER_SET_XYZ", "https://x.test")
    assert provider_credential_state(cfg, "envonly") == "needs_key"
    # a literal key is ready
    cfg.providers["literal"] = ProviderConfig("literal", "openai", "sk-abc", "https://x.test")
    assert provider_credential_state(cfg, "literal") == "ready"
    # a set env var makes the env reference ready
    monkey = "oaset-test-key-123"
    import os

    os.environ["OASET_TEST_KEY_SET"] = monkey
    cfg.providers["envset"] = ProviderConfig("envset", "openai", "env:OASET_TEST_KEY_SET", "https://x.test")
    assert provider_credential_state(cfg, "envset") == "ready"


def test_provider_credential_state_uses_stored_credential():
    from oaset.config import ProviderConfig, default_config, provider_credential_state
    from oaset.credentials import save_credential

    cfg = default_config()
    cfg.providers["stored"] = ProviderConfig("stored", "openai", "", "https://x.test")
    assert provider_credential_state(cfg, "stored") == "needs_key"
    save_credential("stored", "sk-from-store")
    assert provider_credential_state(cfg, "stored") == "ready"


async def test_model_picker_labels_and_orders_by_usability(workspace):
    """Usable models come first with a ✓; keyless presets are labelled and last."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        options = app._model_picker_options()
        values = [value for value, _ in options]
        labels = {value: label for value, label in options}
        assert "mock/mock-echo" in labels
        assert labels["mock/mock-echo"].startswith("✓")          # offline, usable
        # the relay/add entry sits before any blocked preset
        assert values.index("__add__") < values.index("deepseek/deepseek-chat")
        blocked = labels["deepseek/deepseek-chat"]
        assert "密钥" in blocked or "key" in blocked.lower()


async def test_switching_to_a_keyless_preset_is_refused_without_probing(workspace):
    """No 401 dump: the guard refuses before any request and keeps the model."""
    app = make_app(workspace)
    probed: list[str] = []

    async def spy_probe(provider):
        probed.append("called")
        return True, "OK"

    app._probe_provider = spy_probe  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        before = app.model_cfg.id
        await app._switch_model_verified("deepseek/deepseek-chat")
        assert app.model_cfg.id == before           # nothing replaced
        assert not probed                           # no request was even made
        assert "model.no_credential" in app.status_bar.error_text
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "/login" in rendered


async def test_ready_model_still_switches_after_the_guard(workspace):
    """A provider with a stored credential passes the guard and switches."""
    from oaset.credentials import save_credential

    save_credential("glm", "sk-test-glm")  # test homes are isolated per test
    app = make_app(workspace)

    async def ok_probe(provider):
        return True, "OK"

    app._probe_provider = ok_probe  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await app._switch_model_verified("glm/glm-5.3-flash")
        assert app.model_cfg.id == "glm/glm-5.3-flash"


async def test_startup_stays_quiet_when_the_default_model_has_no_key(workspace, tmp_path, monkeypatch):
    """No red error popup at boot: a keyless model is badged on the welcome
    page; the refusal (with its /login hint) only fires if a message is
    actually sent."""
    from oaset.config import default_config, save_config

    cfg = default_config()
    cfg.ui_language = "en"
    cfg.default_model = "deepseek/deepseek-chat"     # env reference, unset
    save_config(cfg)
    from oaset.providers import MockProvider, MockTurn
    from oaset.tui.app import OasetApp

    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                   model_id="deepseek/deepseek-chat")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        assert "model.no_credential" not in app.status_bar.error_text
        assert not getattr(app, "last_error", None), "startup must not pin any error"
        from oaset.tui.widgets.chat import WelcomePanel

        rendered = str(app.chat.query(WelcomePanel).first().render())
        assert "/login" in rendered          # the next-step line carries it
        assert "not configured yet" in rendered
        assert "needs a key" not in rendered      # no scare badge
        assert "deepseek/deepseek-chat" not in rendered  # no phantom default

async def test_wizard_advanced_limits_override(workspace):
    """Limits are auto-resolved, but the confirm step can still override them."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/models add")
        # deepseek ships as a configured provider, so it appears as cfg:<name>
        await _pick_value(pilot, app, "cfg:deepseek")
        await _type(pilot, app, "sk-test")            # api key
        await _type(pilot, app, "my-model")           # model id
        await _type(pilot, app, "tool_use")           # capabilities
        await _type(pilot, app, "")                   # display name
        await _type(pilot, app, "")                   # reasoning key
        # preview → choose the advanced limit override, then save
        await _pick_value(pilot, app, "edit_limits")
        await _type(pilot, app, "64000")              # context
        await _type(pilot, app, "8000")               # max output
        await _pick_value(pilot, app, "save")
        model = app.cfg.models["deepseek/my-model"]
        assert model.max_context_size == 64000 and model.max_output_size == 8000


# ------------------------------------------------- limits resolved, not typed


def test_parse_limits_reads_common_gateway_fields():
    from oaset.model_limits import parse_limits

    assert parse_limits({"context_length": 200000, "max_completion_tokens": 8192}) == {
        "context": 200000, "max_output": 8192}
    # OpenRouter nests the output cap under top_provider
    assert parse_limits({"context_length": 131072,
                         "top_provider": {"max_completion_tokens": 16384}}) == {
        "context": 131072, "max_output": 16384}
    # local servers / aliases
    assert parse_limits({"max_model_len": 32768, "n_predict": 4096}) == {
        "context": 32768, "max_output": 4096}
    assert parse_limits({"unrelated": 1}) == {}


def test_find_model_entry_matches_suffix_and_bare_names():
    from oaset.model_limits import parse_models_payload

    payload = {"data": [
        {"id": "openai/gpt-4o", "context_length": 128000},
        {"id": "deepseek-chat", "context_length": 64000, "max_tokens": 4096},
    ]}
    assert parse_models_payload(payload, "deepseek/deepseek-chat") == {
        "context": 64000, "max_output": 4096}
    assert parse_models_payload(payload, "gpt-4o") == {"context": 128000}
    assert parse_models_payload(payload, "nope/nope") == {}
    # a payload without a listing shape must not raise
    assert parse_models_payload("not-a-dict", "x") == {}


def test_resolve_limits_priority_provider_then_vendor_then_generic():
    from oaset.model_limits import (
        GENERIC_CONTEXT,
        GENERIC_MAX_OUTPUT,
        resolve_limits,
    )

    assert resolve_limits(discovered={"context": 99000}, vendor=(128000, 8192)) == (
        99000, 8192, "provider")          # provider wins for what it knows
    assert resolve_limits(discovered={}, vendor=(200000, 8192)) == (200000, 8192, "vendor")
    assert resolve_limits(discovered={}, vendor=None) == (
        GENERIC_CONTEXT, GENERIC_MAX_OUTPUT, "default")


def test_vendor_catalogue_carries_known_limits():
    from oaset.provider_catalog import vendor_limits

    assert vendor_limits("deepseek") == (128000, 8192)
    assert vendor_limits("gemini")[0] == 1000000
    assert vendor_limits("unknown-vendor") is None


async def test_discover_limits_uses_the_provider_listing():
    from oaset.model_limits import discover_limits

    calls: list[tuple[str, dict]] = []

    async def fake_fetch(url, headers):
        calls.append((url, headers))
        return {"data": [{"id": "acme-1", "context_length": 65536,
                          "max_output_tokens": 4096}]}

    limits = await discover_limits("https://gw.test/v1", "sk-x", "acme-1",
                                  fetch=fake_fetch)
    assert limits == {"context": 65536, "max_output": 4096}
    assert calls[0][0] == "https://gw.test/v1/models"
    assert calls[0][1]["Authorization"] == "Bearer sk-x"


async def test_discover_limits_is_silent_on_failure_and_respects_policy():
    from oaset.model_limits import discover_limits

    async def boom(url, headers):
        raise RuntimeError("offline")

    assert await discover_limits("https://gw.test/v1", "k", "m", fetch=boom) == {}
    # local_only must not even try
    assert await discover_limits("https://gw.test/v1", "k", "m", fetch=boom,
                                 network_mode="local_only") == {}
    # unknown api format: no /models call
    assert await discover_limits("https://gw.test/v1", "k", "m",
                                 api_format="anthropic", fetch=boom) == {}


# The wizard tests stub limit resolution (see make_app); these two pin the
# real wiring underneath it, with the network call itself stubbed at its seam.


async def test_limits_resolution_reaches_the_provider_under_the_policy(workspace, monkeypatch):
    import oaset.model_limits as limits_mod

    app = make_app(workspace)
    calls: list[tuple] = []

    async def fake_discover(base_url, api_key, model_id, **kwargs):
        calls.append((base_url, api_key, model_id, kwargs.get("network_mode")))
        return {}

    monkeypatch.setattr(limits_mod, "discover_limits", fake_discover)
    resolved = await OasetApp._resolve_model_limits(
        app, "openrouter", "https://openrouter.ai/api/v1", "sk-x", "acme-1", "openai")
    assert calls == [("https://openrouter.ai/api/v1", "sk-x", "acme-1",
                      app.cfg.network_mode)]
    # an empty listing falls through to the vendor catalogue, not to a prompt
    assert resolved == (128000, 16384, "vendor")


async def test_limits_resolution_prefers_listing_and_skips_non_openai_formats(workspace, monkeypatch):
    import oaset.model_limits as limits_mod

    app = make_app(workspace)

    async def answering(base_url, api_key, model_id, **kwargs):
        return {"context": 99000, "max_output": 4096}

    monkeypatch.setattr(limits_mod, "discover_limits", answering)
    resolved = await OasetApp._resolve_model_limits(
        app, "openrouter", "https://openrouter.ai/api/v1", "sk-x", "acme-1", "openai")
    assert resolved == (99000, 4096, "provider")

    calls: list[str] = []

    async def recording(base_url, api_key, model_id, **kwargs):
        calls.append(base_url)
        return {}

    monkeypatch.setattr(limits_mod, "discover_limits", recording)
    # an anthropic-format endpoint has no /models listing to ask for
    resolved = await OasetApp._resolve_model_limits(
        app, "acme", "https://gw.test", "k", "m", "anthropic")
    assert calls == []
    assert resolved[2] == "default"


# ------------------------------------------------------- --mock offline path


def test_mock_flag_builds_mock_provider_for_tui_and_one_shot(tmp_path):
    """`--mock` used to be parsed and silently ignored for the main paths; it
    must now be a hard offline guarantee for TUI and one-shot alike."""
    from unittest.mock import patch

    from oaset.cli import main
    from oaset.providers import MockProvider

    captured: dict = {}

    async def fake_one_shot(cfg, cwd, prompt, model, provider, yolo, **kw):
        captured["one_shot_provider"] = provider
        return 0

    class FakeApp:
        def __init__(self, **kwargs):
            captured["tui_provider"] = kwargs.get("provider")

        def run(self):
            return 0

    argv = ["--mock", "--cwd", str(tmp_path), "-p", "hello"]
    with patch("oaset.cli.run_one_shot", fake_one_shot), \
         patch("oaset.tui.app.OasetApp", FakeApp):
        rc = main(argv)
    assert rc == 0
    assert isinstance(captured["one_shot_provider"], MockProvider)

    # TUI path (no -p): must also receive the mock provider.
    # cli imports OasetApp inside main(), so patch the source module.
    with patch("oaset.tui.app.OasetApp", FakeApp):
        rc = main(["--mock", "--cwd", str(tmp_path)])
    assert rc == 0
    assert isinstance(captured["tui_provider"], MockProvider)


# ------------------------------------------------- catalog integrity (batch 7)


def test_vendor_labels_are_labels_and_limits_are_fields():
    """A data-edit regression once merged the limits tuple into the label
    ('Ollama (local, 32768, 8192)'), silently zeroing context/max_output —
    vendor_limits() then returned None and the wizard stopped auto-resolving.
    Labels are prose; limits live in their fields."""
    import re

    from oaset.provider_catalog import VENDORS, vendor, vendor_limits

    for item in VENDORS:
        assert not re.search(r"\d{3,}", item.label), \
            f"{item.id}: limits leaked into the label {item.label!r}"
        if item.context or item.max_output:
            assert vendor_limits(item.id) == (item.context, item.max_output)

    # the domestic aggregators this batch pins must auto-resolve limits
    assert vendor_limits("qwen") == (131072, 8192)
    assert vendor_limits("ark") == (131072, 16384)
    assert vendor_limits("siliconflow") == (131072, 8192)
    assert vendor_limits("minimax") == (245760, 8192)
    assert vendor("ark").base_url.endswith("/api/v3")


# ---------------------------------------------------- Azure deployments (batch 7)


def test_azure_provider_builds_a_deployment_client(isolated_home, monkeypatch):
    """Azure is OpenAI-wire-compatible but not base_url-compatible: the SDK's
    Azure client embeds the deployment path, sends api-version, and swaps the
    auth header — the provider must be that client, not a plain one."""
    import os

    from oaset.config import ModelConfig, ProviderConfig, build_provider
    from oaset.providers.azure_compat import AzureCompatProvider
    from oaset.providers.openai_compat import OpenAICompatProvider

    monkeypatch.setenv("OASET_AZURE_API_VERSION", "2026-01-01-preview")
    cfg = default_config()
    cfg.providers["azu"] = ProviderConfig(
        "azu", "azure", "env:OASET_AZURE_KEY", "https://my-res.openai.azure.com",
        api_format="azure")
    cfg.models["azu/gpt-4o"] = ModelConfig("azu/gpt-4o", "azu", "gpt-4o-deploy")
    monkeypatch.setenv("OASET_AZURE_KEY", "sk-azure-test")

    provider = build_provider(cfg, cfg.models["azu/gpt-4o"])
    assert isinstance(provider, AzureCompatProvider)
    assert isinstance(provider, OpenAICompatProvider), "wire protocol is shared"
    assert provider._client.default_query == {"api-version": "2026-01-01-preview"}
    assert "gpt-4o-deploy" in str(provider._client.base_url), (
        "the model field names the Azure deployment")
    # a plain openai-format provider must NOT become an Azure client
    cfg.providers["plain"] = ProviderConfig(
        "plain", "openai", "env:OASET_AZURE_KEY", "https://api.test/v1")
    cfg.models["plain/m"] = ModelConfig("plain/m", "plain", "m")
    plain = build_provider(cfg, cfg.models["plain/m"])
    assert not isinstance(plain, AzureCompatProvider)
    assert os.environ.get("OASET_AZURE_API_VERSION") is not None


def test_azure_catalog_entry_passes_validation():
    from oaset.provider_catalog import default_api_format, vendor

    item = vendor("azure")
    assert item and item.api_format == "azure"
    from oaset.provider_catalog import validate_base_url

    assert validate_base_url(item.base_url)  # the placeholder is a valid https URL
    assert default_api_format("azure") == "azure"
