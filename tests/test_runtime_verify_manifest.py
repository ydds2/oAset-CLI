"""models verify / plugin manifest / RuntimeFactory coverage."""

from __future__ import annotations

import json

import pytest

from oaset import plugins as plugins_mod
from oaset.plugins import load_plugins
from oaset.providers import MockProvider, MockTurn
from oaset.tools import ToolRegistry

# ------------------------------------------------------------------ models verify

async def test_models_verify_ok_json(capsys):
    from oaset.cli import cmd_models_verify
    from oaset.config import default_config

    cfg = default_config()
    provider = MockProvider([MockTurn(content_chunks=["OK"], finish_reason="stop")])
    rc = await cmd_models_verify(cfg, "mock/mock-echo", json_out=True, provider=provider)
    assert rc == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["ok"] is True
    assert verdict["model"] == "mock/mock-echo"
    assert verdict["reply"] == "OK"
    assert "elapsed_ms" in verdict and "endpoint" in verdict


async def test_models_verify_unknown_model_fails(capsys):
    from oaset.cli import cmd_models_verify
    from oaset.config import default_config

    rc = await cmd_models_verify(default_config(), "nope/nope")
    assert rc == 2
    assert "error" in capsys.readouterr().err


async def test_models_verify_local_only_blocked(capsys):
    """local_only must refuse a real HTTP probe with a structured verdict (exit 1).

    Uses a genuine (non-mock) endpoint: the offline mock short-circuits model
    verification by design and never touches the network, so it cannot exercise
    the policy gate any more (it used to be declared as a localhost HTTP URL).
    """
    from oaset.cli import cmd_models_verify
    from oaset.config import ModelConfig, ProviderConfig, default_config

    cfg = default_config()
    cfg.network_mode = "local_only"
    cfg.providers["probe"] = ProviderConfig(
        "probe", "openai", "sk-test", "https://probe.invalid/v1")
    cfg.models["probe/m1"] = ModelConfig(
        "probe/m1", "probe", "m1", 8000, display_name="Probe")
    rc = await cmd_models_verify(cfg, "probe/m1", json_out=True)
    assert rc == 1
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["ok"] is False
    assert "network" in verdict.get("error", "")


# ------------------------------------------------------------------ plugin manifest

MANIFEST_PLUGIN = '''
OASET_MANIFEST = {
    "version": "2.1.0",
    "description": "demo plugin",
    "requires_oaset": ">=0.0.1",
    # "tools"/"commands" are enforced capabilities (SUPPLY-01); extra tokens
    # like "shell"/"fs-read" stay display-only metadata
    "permissions": ["tools", "shell", "fs-read"],
}

from oaset.tools.base import Tool, ToolResult

class _M(Tool):
    name = "mantool"
    description = "manifest probe"
    permission = "read"
    parameters = {}

    async def run(self, args, ctx):
        return ToolResult("ok")

def register(api):
    api.add_tool(_M())
'''


@pytest.fixture()
def plugins_home(tmp_path, monkeypatch):
    """Plugin-dir isolation ONLY. The old name (`isolated_home`) SHADOWED
    the conftest's autouse env isolation for this whole module — the SDK
    factory test then wrote real sessions into ~/.oaset on every run."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(plugins_mod, "oaset_home", lambda: home)
    return home


def test_manifest_validated_and_recorded(plugins_home, tmp_path):
    user_plugins = plugins_home / "plugins"
    user_plugins.mkdir()
    (user_plugins / "manifested.py").write_text(MANIFEST_PLUGIN, encoding="utf-8")

    records: list = []
    registry = ToolRegistry()
    names = load_plugins(tmp_path, registry, {}, lambda s: None, loaded=records)
    assert names == ["manifested"]
    manifest = records[0].manifest
    assert manifest["version"] == "2.1.0"
    assert set(manifest["permissions"]) == {"tools", "shell", "fs-read"}
    assert manifest.get("_unknown_keys") is None
    assert records[0].tools == ["mantool"]


def test_manifest_invalid_types_flagged_not_fatal(plugins_home, tmp_path):
    user_plugins = plugins_home / "plugins"
    user_plugins.mkdir()
    (user_plugins / "badman.py").write_text(
        MANIFEST_PLUGIN.replace('"2.1.0"', "21"), encoding="utf-8")  # version as int

    records: list = []
    registry = ToolRegistry()
    log: list[str] = []
    names = load_plugins(tmp_path, registry, {}, log.append, loaded=records)
    assert names == ["badman"]  # loads anyway — bad manifest must not brick the host
    assert records[0].manifest.get("_invalid")
    assert any("OASET_MANIFEST" in line for line in log)


def test_manifest_unknown_keys_reported(plugins_home, tmp_path):
    user_plugins = plugins_home / "plugins"
    user_plugins.mkdir()
    (user_plugins / "extra.py").write_text(
        MANIFEST_PLUGIN + '\nOASET_MANIFEST["future_field"] = "x"\n', encoding="utf-8")

    records: list = []
    load_plugins(tmp_path, ToolRegistry(), {}, lambda s: None, loaded=records)
    assert "future_field" in records[0].manifest.get("_unknown_keys", [])


# ------------------------------------------------------------------ runtime factory

def test_build_registry_applies_toggles_and_network(tmp_path):
    from oaset.config import default_config
    from oaset.runtime import build_registry

    cfg = default_config()
    cfg.disabled_tools = ["web_fetch"]
    cfg.network_mode = "local_only"
    registry = build_registry(cfg, tmp_path)
    assert "web_fetch" in registry.disabled
    assert registry.ctx.network_mode == "local_only"
    assert registry.ctx.session_state["shell_config"]["backend"] == "local"


def test_build_runtime_resolves_model_and_chain(tmp_path):
    from oaset.config import default_config
    from oaset.runtime import build_runtime

    cfg = default_config()
    rt = build_runtime(cfg, tmp_path, model_id="mock/mock-echo")
    assert rt.model_cfg.id == "mock/mock-echo"
    assert rt.provider is not None
    assert isinstance(rt.fallbacks, list)
    assert "general" in rt.agents or not rt.agents  # agents dir may be empty in tests
    assert rt.registry.ctx.cwd == tmp_path


def test_sdk_and_runtime_factory_agree(tmp_path):
    """SDK assembly must equal build_runtime's (the drift that motivated it)."""
    from oaset.config import default_config
    from oaset.runtime import build_runtime
    from oaset.sdk import Oaset

    cfg = default_config()
    cfg.disabled_tools = ["web_fetch"]
    agent = Oaset(config=cfg, cwd=tmp_path, model="mock/mock-echo")
    rt = build_runtime(cfg, tmp_path, model_id="mock/mock-echo",
                       mode="default", tools=True)
    assert set(agent.registry.disabled) == set(rt.registry.disabled)
    assert agent.registry.ctx.network_mode == rt.registry.ctx.network_mode
    assert agent.model_cfg.id == rt.model_cfg.id


def test_build_registry_tools_false_empties_active(tmp_path):
    from oaset.config import default_config
    from oaset.runtime import build_registry

    registry = build_registry(default_config(), tmp_path, tools=False)
    assert registry.tools == {}
    assert registry._all  # definitions remain for later enable
