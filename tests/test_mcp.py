"""MCP client + manager + tool adapter against the bundled fake stdio server."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from oaset.config import default_config
from oaset.mcp import McpConnection, McpManager, load_server_specs
from oaset.mcp.client import sanitize_tool_name
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.tools import AutoGate, ToolContext, ToolRegistry
from oaset.tui.widgets.tool_card import ToolCard
from tests.test_app_tui import chat_text, wait_until

FAKE = Path(__file__).resolve().parent / "fake_mcp_server.py"


def make_conn(name="fake") -> McpConnection:
    return McpConnection(name=name, command=sys.executable, args=[str(FAKE)], timeout=10)


async def test_connection_initialize_list_call():
    conn = make_conn()
    await conn.start()
    try:
        tools = await conn.list_tools()
        assert [t.name for t in tools] == ["echo", "add"]
        assert "Echo" in tools[0].description
        result = await conn.call_tool("echo", {"text": "hello mcp"})
        assert result.text == "echo: hello mcp"
        assert result.is_error is False
        result = await conn.call_tool("add", {"a": 2, "b": 3.5})
        assert result.text == "5.5"
        result = await conn.call_tool("echo", {"text": "fail"})
        assert result.is_error is True
        assert await conn.ping() is True
    finally:
        await conn.close()


async def test_unknown_tool_is_error():
    conn = make_conn()
    await conn.start()
    try:
        result = await conn.call_tool("nope", {})
        assert result.is_error and "unknown tool" in result.text
    finally:
        await conn.close()


def test_load_server_specs(isolated_home, workspace):
    (isolated_home / "mcp.json").write_text(
        json.dumps({"mcpServers": {"home-server": {"command": "cmd", "args": ["a"]}}}),
        encoding="utf-8",
    )
    project = workspace / ".oaset"
    project.mkdir()
    (project / "mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "proj-server": {"command": "python", "args": ["-x"], "env": {"K": "V"}},
                "home-server": {"command": "override"},
            }
        }),
        encoding="utf-8",
    )
    specs = {s.name: s for s in load_server_specs(isolated_home, cwd=workspace)}
    assert set(specs) == {"home-server", "proj-server"}
    assert specs["home-server"].command == "override"  # project wins
    assert specs["proj-server"].env == {"K": "V"}
    assert specs["proj-server"].args == ["-x"]
    # origin tracking + trust: user config trusted by default, project not
    assert specs["proj-server"].origin == "project"
    assert specs["home-server"].origin == "project"  # project override wins wholesale
    assert not specs["proj-server"].trusted
    # a workdir without project config leaves the user-level entry trusted
    user_specs = {s.name: s for s in load_server_specs(isolated_home, cwd=workspace / "empty")}
    assert user_specs["home-server"].origin == "user"
    assert user_specs["home-server"].trusted


async def test_manager_registers_tools_into_registry(isolated_home):
    (isolated_home / "mcp.json").write_text(
        json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [str(FAKE)]}}}),
        encoding="utf-8",
    )
    manager = McpManager(isolated_home)
    results = await manager.start_all()
    try:
        assert results == [("fake", None)]
        adapters = manager.adapters()
        assert {a.name for a in adapters} == {"mcp__fake__echo", "mcp__fake__add"}
        registry = ToolRegistry(gate=AutoGate())
        registry.bind(ToolContext(cwd=Path("."), mode="auto", output_limit=500))
        for adapter in adapters:
            registry.add_tool(adapter)
        schema = registry.get("mcp__fake__echo").schema()
        assert schema["function"]["parameters"]["properties"]["text"]["type"] == "string"
        result = await registry.dispatch("mcp__fake__echo", json.dumps({"text": "via registry"}))
        assert not result.is_error and "echo: via registry" in result.output
        # tool-count parity on the wire: tool available to the model
        assert "mcp__fake__add" in [s["function"]["name"] for s in registry.schemas()]
    finally:
        await manager.shutdown()
    assert manager.connections == {}


def test_sanitize_and_status():
    assert sanitize_tool_name("my-srv", "do.thing") == "mcp__my_srv__do_thing"
    from oaset.mcp import McpManager

    empty = McpManager(Path("."))
    assert "no servers" in empty.status_line()


async def test_manager_isolates_failures(isolated_home):
    (isolated_home / "mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "fake": {"command": sys.executable, "args": [str(FAKE)]},
                "broken": {"command": "definitely-not-a-real-binary-xyz"},
            }
        }),
        encoding="utf-8",
    )
    manager = McpManager(isolated_home)
    results = dict(await manager.start_all())
    try:
        assert results["fake"] is None
        assert results["broken"] is not None  # isolated, did not kill 'fake'
        assert any("mcp__fake__echo" == a.name for a in manager.adapters())
    finally:
        await manager.shutdown()


async def test_mcp_tool_inside_tui_agent_loop(workspace):
    """Full in-app path: mcp.json → startup worker → agent worker dispatch → gate modal."""
    import json as _json

    from oaset.tui.app import OasetApp
    from oaset.tui.widgets.inline import InlinePermission

    project = workspace / ".oaset"
    project.mkdir()
    (project / "mcp.json").write_text(
        _json.dumps({
            "mcpServers": {"fake": {"command": sys.executable, "args": [str(FAKE)]}}
        }),
        encoding="utf-8",
    )
    # A3 trust model: project servers need an explicit trust pin before the
    # TUI will spawn them (they execute third-party commands from the repo).
    from oaset.mcp.manager import trust_project_server
    from oaset.utils import oaset_home

    assert trust_project_server(oaset_home(), workspace, "fake") is not None
    script = [
        MockTurn(tool_calls=[MockToolCall("mcp__fake__echo", {"text": "hi from model"})]),
        MockTurn(content_chunks=["mcp said hi"]),
    ]
    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace, provider=MockProvider(script), model_id="mock/mock-echo")
    async with app.run_test(size=(110, 36)) as pilot:
        # the startup MCP worker must finish registering tools before we submit
        assert await wait_until(pilot, lambda: "mcp__fake__echo" in app.registry.tools)
        app.input_area.load_text("call the mcp tool")
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: list(app.query(InlinePermission)))
        await pilot.press("y")
        assert await wait_until(pilot, lambda: "mcp said hi" in chat_text(app))
        cards = [c for c in app.chat.query(ToolCard)
                 if "mcp__fake__echo" in c.title_text or "echo: hi from model" in (c.raw_output or "")]
        assert cards and "echo: hi from model" in cards[-1].raw_output


# ------------------------------------------------------------- A2: env allowlist

def test_build_mcp_env_allowlist_blocks_ambient_secrets(monkeypatch):
    from oaset.mcp.client import build_mcp_env

    monkeypatch.setenv("PATH", r"C:\bin")
    monkeypatch.setenv("TEMP", r"C:\tmp")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "also-secret")

    env = build_mcp_env()
    assert env["PATH"] == r"C:\bin"          # allowlisted ambient var passes
    assert env["TEMP"] == r"C:\tmp"
    assert "OPENAI_API_KEY" not in env       # ambient secrets never inherit
    assert "AWS_SECRET_ACCESS_KEY" not in env

    env = build_mcp_env({"OPENAI_API_KEY": "sk-declared"})
    assert env["OPENAI_API_KEY"] == "sk-declared"  # explicit declaration wins
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_build_mcp_env_inherit_escape_hatch(monkeypatch):
    from oaset.mcp.client import build_mcp_env

    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret")
    env = build_mcp_env({"EXTRA": "1"}, inherit_env=True)
    assert env["OPENAI_API_KEY"] == "sk-super-secret"
    assert env["EXTRA"] == "1"


# ------------------------------------------------------------- A3: project trust

async def test_project_server_not_started_without_trust(isolated_home, workspace):
    (workspace / ".oaset").mkdir(exist_ok=True)
    (workspace / ".oaset" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [str(FAKE)]}}}),
        encoding="utf-8",
    )
    manager = McpManager(isolated_home, cwd=workspace)
    results = dict(await manager.start_all())
    try:
        assert results["fake"] is None           # skipped, not an error
        assert "fake" not in manager.connections  # nothing was spawned
        assert manager.untrusted == ["fake"]
        assert "fake" in manager.status_line()
        assert "oaset mcp trust" in manager.status_line()
    finally:
        await manager.shutdown()


async def test_project_server_trust_pin_and_invalidation(isolated_home, workspace):
    from oaset.mcp.manager import is_project_server_trusted, load_server_specs, trust_project_server

    project = workspace / ".oaset"
    project.mkdir(exist_ok=True)
    cfg_path = project / "mcp.json"
    cfg_path.write_text(
        json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [str(FAKE)]}}}),
        encoding="utf-8",
    )
    manager = McpManager(isolated_home, cwd=workspace)
    await manager.start_all()
    await manager.shutdown()
    assert manager.untrusted == ["fake"]

    # pin trust → server starts like a user-level one
    assert trust_project_server(isolated_home, workspace, "fake") is not None
    specs = {s.name: s for s in load_server_specs(isolated_home, cwd=workspace)}
    assert specs["fake"].trusted is True
    manager = McpManager(isolated_home, cwd=workspace)
    results = dict(await manager.start_all())
    try:
        assert results["fake"] is None and "fake" in manager.connections
        assert "mcp__fake__echo" in {a.name for a in manager.adapters()}
    finally:
        await manager.shutdown()

    # editing the config invalidates the pin (same rule as plugin sha256)
    cfg_path.write_text(
        json.dumps({"mcpServers": {"fake": {"command": sys.executable,
                                            "args": [str(FAKE), "--changed"]}}}),
        encoding="utf-8",
    )
    specs = {s.name: s for s in load_server_specs(isolated_home, cwd=workspace)}
    assert specs["fake"].trusted is False
    assert not is_project_server_trusted(isolated_home, workspace, specs["fake"])


def test_trust_unknown_project_server_fails(isolated_home, workspace):
    from oaset.mcp.manager import trust_project_server

    assert trust_project_server(isolated_home, workspace, "nope") is None


async def test_elicitation_field_times_out_instead_of_hanging(workspace):
    """Freeze-fix regression: an unnoticed MCP elicitation input must time
    out (reported as cancelled), never hang the turn forever. ask_user got
    this rule in P0-6; elicitation takes the same deal."""
    from oaset.tui.app import OasetApp
    from oaset.tui.widgets.inline import InlineInput

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    app.ELICIT_INPUT_TIMEOUT = 0.2
    async with app.run_test(size=(100, 30)) as pilot:
        result = await app._elicit_field("server question", "field")
        assert result is None  # timeout maps to cancel
        # poll for the removal instead of a fixed 0.1s pause: under a loaded
        # machine the cleanup routinely needs longer, and a fixed sleep
        # turned this into a load-sensitive false failure
        for _ in range(50):
            if not list(app.query(InlineInput)):
                break
            await pilot.pause(0.1)
        assert not list(app.query(InlineInput)), "prompt widget must be cleaned up"
