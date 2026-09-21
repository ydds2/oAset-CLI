"""Project-plugin trust gate (PLG-01 first step).

User-directory plugins load by default; project-directory plugins are
skipped until `oaset trust-plugin <name>` pins their sha256, and skipped
again when the file changes.
"""

from __future__ import annotations

import pytest

from oaset import plugins as plugins_mod
from oaset.plugins import PluginCommand, is_trusted, load_plugins, reload_plugins
from oaset.tools import ToolRegistry

PLUGIN_BODY = '''
from oaset.tools.base import Tool, ToolResult

class _P(Tool):
    name = "trusttool"
    description = "trust probe"
    permission = "read"
    parameters = {}

    async def run(self, args, ctx):
        return ToolResult("ok")

def register(api):
    api.add_tool(_P())
'''


@pytest.fixture()
def isolated_plugin_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(plugins_mod, "oaset_home", lambda: home)
    return home


def test_global_plugin_loads_without_trust(isolated_plugin_home, tmp_path):
    user_plugins = isolated_plugin_home / "plugins"
    user_plugins.mkdir()
    (user_plugins / "mine.py").write_text(PLUGIN_BODY, encoding="utf-8")

    registry = ToolRegistry()
    commands: dict[str, PluginCommand] = {}
    log: list[str] = []
    names = load_plugins(tmp_path, registry, commands, log.append)
    assert names == ["mine"]
    assert "trusttool" in registry._all


def test_project_plugin_skipped_until_trusted(isolated_plugin_home, tmp_path):
    project_plugins = tmp_path / ".oaset" / "plugins"
    project_plugins.mkdir(parents=True)
    plugin_file = project_plugins / "proj.py"
    plugin_file.write_text(PLUGIN_BODY, encoding="utf-8")

    registry = ToolRegistry()
    commands: dict[str, PluginCommand] = {}
    log: list[str] = []

    # 1. untrusted → skipped, with an actionable hint
    names = load_plugins(tmp_path, registry, commands, log.append)
    assert names == []
    assert "trusttool" not in registry._all
    assert any("trust-plugin proj" in line for line in log)

    # 2. trusted (pin hash the same way the CLI does) → loads
    from oaset.plugins import trust_plugin_file

    trust_plugin_file(plugin_file)
    assert is_trusted("proj", plugin_file)
    names = load_plugins(tmp_path, registry, commands, log.append)
    assert names == ["proj"]
    assert "trusttool" in registry._all

    # 3. file modified → trust invalidated → skipped again
    plugin_file.write_text(PLUGIN_BODY + "\n# tampered\n", encoding="utf-8")
    assert not is_trusted("proj", plugin_file)
    registry2 = ToolRegistry()
    names = load_plugins(tmp_path, registry2, {}, log.append)
    assert names == []
    assert "trusttool" not in registry2._all


def test_hot_reload_respects_trust_gate(isolated_plugin_home, tmp_path):
    project_plugins = tmp_path / ".oaset" / "plugins"
    project_plugins.mkdir(parents=True)
    plugin_file = project_plugins / "proj.py"
    plugin_file.write_text(PLUGIN_BODY, encoding="utf-8")

    registry = ToolRegistry()
    commands: dict[str, PluginCommand] = {}
    records: list = []
    log: list[str] = []

    assert load_plugins(tmp_path, registry, commands, log.append, loaded=records) == []
    from oaset.plugins import trust_plugin_file

    trust_plugin_file(plugin_file)
    names = reload_plugins(tmp_path, registry, commands, log.append, records)
    assert names == ["proj"]
    assert "trusttool" in registry._all


def test_trust_rejects_path_traversal(tmp_path):
    from oaset.plugins import is_valid_plugin_name

    assert is_valid_plugin_name("demo") is True
    assert is_valid_plugin_name("../demo") is False
    assert is_valid_plugin_name("a/b") is False
    assert is_valid_plugin_name("") is False
    assert is_valid_plugin_name("a\\b") is False


NEEDS_NEWER = '''
OASET_MANIFEST = {"version": "1.0", "requires_oaset": ">=999.0.0"}
from oaset.tools.base import Tool, ToolResult

class _N(Tool):
    name = "needstool"
    description = "needs newer host"
    permission = "read"
    parameters = {}
    async def run(self, args, ctx):
        return ToolResult("x")

def register(api):
    api.add_tool(_N())
'''

NEEDS_OLDEST = NEEDS_NEWER.replace(">=999.0.0", ">=0.0.1")
BAD_CONSTRAINT = NEEDS_NEWER.replace(">=999.0.0", "~=weird")


def test_requires_oaset_blocks_incompatible_plugin(isolated_plugin_home, tmp_path):
    user_plugins = isolated_plugin_home / "plugins"
    user_plugins.mkdir()
    (user_plugins / "newer.py").write_text(NEEDS_NEWER, encoding="utf-8")

    registry = ToolRegistry()
    log: list[str] = []
    names = load_plugins(tmp_path, registry, {}, log.append)
    assert names == []
    assert "needstool" not in registry._all
    assert any("requires oaset" in line for line in log)


def test_requires_oaset_allows_compatible_plugin(isolated_plugin_home, tmp_path):
    user_plugins = isolated_plugin_home / "plugins"
    user_plugins.mkdir()
    (user_plugins / "old.py").write_text(NEEDS_OLDEST, encoding="utf-8")

    registry = ToolRegistry()
    names = load_plugins(tmp_path, registry, {}, lambda s: None)
    assert names == ["old"]
    assert "needstool" in registry._all


def test_requires_oaset_unparseable_constraint_skips(isolated_plugin_home, tmp_path):
    user_plugins = isolated_plugin_home / "plugins"
    user_plugins.mkdir()
    (user_plugins / "weird.py").write_text(BAD_CONSTRAINT, encoding="utf-8")

    registry = ToolRegistry()
    log: list[str] = []
    names = load_plugins(tmp_path, registry, {}, log.append)
    assert names == []
    assert any("无法解析" in line for line in log)


# ------------------------------------- capability isolation via manifest (01c)

ISOLATED_BODY = '''
from oaset.tools.base import Tool, ToolResult

class _P(Tool):
    name = "isotool"
    description = "isolation probe"
    permission = "read"
    parameters = {}

    async def run(self, args, ctx):
        return ToolResult("ok")

def register(api):
    api.add_tool(_P())          # needs "tools"
    api.add_command("isocmd", "probe", lambda app, args: None)  # needs "commands"
'''


def _write_isolated_plugin(home, body):
    user_plugins = home / "plugins"
    user_plugins.mkdir(exist_ok=True)
    (user_plugins / "iso.py").write_text(body, encoding="utf-8")


def _fresh():
    return ToolRegistry(), {}, []


def test_manifest_permissions_act_as_allowlist(isolated_plugin_home, tmp_path):
    """Declared permissions = capability allowlist: add_tool needs "tools",
    add_command needs "commands"; undeclared calls are refused, logged, and
    the plugin load reports the failure instead of silently running."""
    _write_isolated_plugin(isolated_plugin_home, (
        "OASET_MANIFEST = {'version': '1.0', 'permissions': ['tools']}\n"
        + ISOLATED_BODY))
    registry, commands, log = _fresh()
    names = load_plugins(tmp_path, registry, commands, log.append)
    assert names == ["iso"]  # register() partially succeeded then raised
    assert "isotool" in registry.tools          # declared: registered
    assert "isocmd" not in commands             # undeclared: refused
    assert any("权限未声明 'commands'" in line for line in log)


def test_plugin_without_manifest_keeps_legacy_surface(isolated_plugin_home, tmp_path):
    """No manifest -> the unrestricted legacy surface (back-compat), and the
    plugin is visibly unrestricted via api.permissions is None."""
    _write_isolated_plugin(isolated_plugin_home, ISOLATED_BODY)
    registry, commands, log = _fresh()
    names = load_plugins(tmp_path, registry, commands, log.append)
    assert names == ["iso"]
    assert "isotool" in registry.tools and "isocmd" in commands


def test_fully_declared_manifest_grants_both_capabilities(isolated_plugin_home, tmp_path):
    _write_isolated_plugin(isolated_plugin_home, (
        "OASET_MANIFEST = {'version': '1.0', 'permissions': ['tools', 'commands']}\n"
        + ISOLATED_BODY))
    registry, commands, log = _fresh()
    names = load_plugins(tmp_path, registry, commands, log.append)
    assert names == ["iso"]
    assert "isotool" in registry.tools and "isocmd" in commands


def test_empty_permissions_allowlist_blocks_everything(isolated_plugin_home, tmp_path):
    """Declaring `permissions: []` is the strongest stance: the plugin may
    only use api.log — anything else is refused."""
    _write_isolated_plugin(isolated_plugin_home, (
        "OASET_MANIFEST = {'version': '1.0', 'permissions': []}\n"
        + ISOLATED_BODY))
    registry, commands, log = _fresh()
    names = load_plugins(tmp_path, registry, commands, log.append)
    assert names == ["iso"]  # the register() call raises -> plugin fails visibly
    assert "isotool" not in registry.tools
    assert "isocmd" not in commands
    assert any("权限未声明 'tools'" in line for line in log)
