"""TUI management surfaces: MCP server admin, skills admin, command registry.

These are the "complete command system" guards: every command must be
discoverable (metadata + i18n effect), every arg-taking command must have a
form/ArgSpec, and the MCP/skills admin rules are unit-tested without a display.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oaset.tui.mcp_admin import (
    McpAdminError,
    add_server,
    list_servers,
    mcp_path,
    read_servers,
    remove_server,
    server_entry,
    set_enabled,
)

# --------------------------------------------------------------- MCP admin


def test_mcp_paths_by_scope(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "ws"
    cwd.mkdir(parents=True)
    assert mcp_path(home, cwd, "project") == cwd / ".oaset" / "mcp.json"
    assert mcp_path(home, cwd, "user") == home / "mcp.json"
    with pytest.raises(McpAdminError):
        mcp_path(home, cwd, "somewhere")


def test_mcp_add_enable_disable_remove_roundtrip(tmp_path):
    path = tmp_path / ".oaset" / "mcp.json"
    add_server(path, "files", server_entry(command="npx", args=["-y", "fs-server"]))
    add_server(path, "remote", server_entry(url="https://mcp.test/sse"))

    servers = read_servers(path)
    assert set(servers) == {"files", "remote"}
    assert servers["files"]["command"] == "npx"
    assert servers["remote"]["url"] == "https://mcp.test/sse"

    set_enabled(path, "files", False)
    assert read_servers(path)["files"]["enabled"] is False
    set_enabled(path, "files", True)
    assert "enabled" not in read_servers(path)["files"]

    remove_server(path, "remote")
    assert set(read_servers(path)) == {"files"}


def test_mcp_add_rejects_duplicates_and_bad_entries(tmp_path):
    path = tmp_path / "mcp.json"
    add_server(path, "a", server_entry(command="run"))
    with pytest.raises(McpAdminError) as excinfo:
        add_server(path, "a", server_entry(command="run"))
    assert excinfo.value.code == "mcp.duplicate"
    add_server(path, "a", server_entry(command="run2"), overwrite=True)
    assert read_servers(path)["a"]["command"] == "run2"

    with pytest.raises(McpAdminError):
        server_entry()  # neither url nor command
    with pytest.raises(McpAdminError):
        server_entry(url="ftp://x", command="")
    with pytest.raises(McpAdminError):
        server_entry(url="https://x", command="y")  # both
    with pytest.raises(McpAdminError):
        add_server(path, "bad name", server_entry(command="x"))


def test_mcp_list_merges_scopes_with_project_winning(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "ws"
    cwd.mkdir(parents=True)
    add_server(mcp_path(home, cwd, "user"), "shared", server_entry(command="user-cmd"))
    add_server(mcp_path(home, cwd, "user"), "useronly", server_entry(url="https://u.test"))
    add_server(mcp_path(home, cwd, "project"), "shared", server_entry(command="project-cmd"))

    rows = {r.name: r for r in list_servers(home, cwd)}
    assert rows["shared"].scope == "project"
    assert rows["shared"].target == "project-cmd"
    assert rows["useronly"].scope == "user"
    assert rows["useronly"].transport == "http"


def test_mcp_unknown_server_operations_are_rejected(tmp_path):
    path = tmp_path / "mcp.json"
    with pytest.raises(McpAdminError) as excinfo:
        set_enabled(path, "ghost", False)
    assert excinfo.value.code == "mcp.unknown_server"
    with pytest.raises(McpAdminError):
        remove_server(path, "ghost")


def test_mcp_tool_filter_survives_roundtrip(tmp_path):
    path = tmp_path / "mcp.json"
    add_server(path, "filtered", server_entry(
        url="https://x.test", enabled_tools=["read", "write"], disabled_tools=["delete"]))
    entry = read_servers(path)["filtered"]
    assert entry["enabledTools"] == ["read", "write"]
    assert entry["disabledTools"] == ["delete"]
    row = list_servers(tmp_path, tmp_path)[0]
    assert "enabled: read,write" in row.tool_filter


# ------------------------------------------------------------- skills admin


def test_skill_create_show_and_delete(tmp_path):
    from oaset.skills import create_skill, delete_skill, find_skill, load_skills

    path = create_skill(tmp_path, "release-check", "How to cut a release", scope="project")
    assert path.is_file() and path.name == "SKILL.md"
    skill = find_skill(tmp_path, "release-check")
    assert skill is not None and "release" in skill.name.lower()
    assert "How to cut a release" in skill.body
    assert [s.name for s in load_skills(tmp_path)]

    with pytest.raises(ValueError):
        create_skill(tmp_path, "release-check", scope="project")  # no overwrite
    create_skill(tmp_path, "release-check", scope="project", overwrite=True)

    removed = delete_skill(tmp_path, "release-check")
    assert removed is not None and not removed.exists()
    assert find_skill(tmp_path, "release-check") is None


def test_system_prompt_lists_skill_paths(tmp_path):
    from oaset.agent.prompts import build_system_prompt
    from oaset.skills import create_skill, load_skills

    path = create_skill(tmp_path, "release-check", "How to cut a release", scope="project")
    prompt = build_system_prompt(tmp_path, skills=load_skills(tmp_path))
    assert "release-check" in prompt
    assert str(path) in prompt
    assert "read_file" in prompt


def test_skill_create_validates_names(tmp_path):
    from oaset.skills import create_skill

    with pytest.raises(ValueError):
        create_skill(tmp_path, "", scope="project")
    with pytest.raises(ValueError):
        create_skill(tmp_path, "bad/name", scope="project")
    with pytest.raises(ValueError):
        create_skill(tmp_path, "x", scope="nowhere")


# ------------------------------------------------------- command completeness


def test_every_command_is_discoverable_and_well_formed():
    from oaset.tui.commands import COMMANDS, RISK_DANGER, RISK_WRITE, arg_spec_for, effect_display

    names = [c.name for c in COMMANDS]
    assert len(names) == len(set(names)), "duplicate command names"
    for cmd in COMMANDS:
        assert cmd.description, f"/{cmd.name} has no description"
        assert cmd.category, f"/{cmd.name} has no category"
        assert cmd.risk in ("safe", "read", "write", "danger"), cmd.risk
        if cmd.risk in (RISK_WRITE, RISK_DANGER):
            assert effect_display(cmd), f"/{cmd.name} writes but has no effect line"
        if cmd.requires_args:
            assert arg_spec_for(cmd.name) is not None, \
                f"/{cmd.name} requires args but has no ArgSpec form"


def test_every_command_has_a_dispatchable_handler():
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn
    from oaset.tui.app import OasetApp
    from oaset.tui.commands import COMMANDS

    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=Path.cwd(),
                   provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                   model_id="mock/mock-echo")
    missing = [c.name for c in COMMANDS
               if not hasattr(app, "cmd_" + c.name.replace("-", "_"))]
    assert not missing, f"commands without a handler: {missing}"


def test_new_management_commands_are_registered():
    from oaset.tui.commands import COMMANDS, alias_map, arg_spec_for

    names = {c.name for c in COMMANDS}
    assert {"mcp", "skills", "copy", "paste"} <= names
    # /models merged into /model as an alias — one surface, one registry entry
    assert "models" not in names
    assert alias_map()["models"] == "model"
    for name in ("mcp", "skills", "update", "model", "roots"):
        assert arg_spec_for(name) is not None, f"/{name} should offer a form"
    mcp = next(c for c in COMMANDS if c.name == "mcp")
    assert "add" in mcp.subcommands and "remove" in mcp.subcommands


def test_palette_renders_every_command_without_markup_errors():
    """Command usage like [a|b] must not break Rich markup in the palette."""
    from rich.markup import escape

    from oaset.tui.commands import COMMANDS

    for cmd in COMMANDS:
        row = f"{cmd.name:<20}{cmd.usage or ''}{cmd.category}"
        escape(row)  # must not raise; palette escapes rows before rendering
