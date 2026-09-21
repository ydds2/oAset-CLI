"""LSP client tests: framing, initialize/didOpen loop, diagnostics injection,
lsp_diagnostics tool, config loading. All offline via the bundled fake server."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from oaset.lsp import LspConnection, LspManager, load_lsp_config
from oaset.tools import AutoGate, ToolContext, ToolRegistry

FAKE = Path(__file__).resolve().parent / "fake_lsp_server.py"


@pytest.fixture
def lsp_config(isolated_home):
    def _write(lang_command: dict):
        lines = ["[lsp]"]
        for lang, argv in lang_command.items():
            lines.append(f"{lang} = {json.dumps(argv)}")
        (isolated_home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return _write


async def test_lsp_client_roundtrip(workspace):
    conn = LspConnection("python", [sys.executable, str(FAKE)], cwd=workspace)
    await conn.start()
    try:
        await conn.initialize()
        target = workspace / "mod.py"
        target.write_text("x = undefined_name\n", encoding="utf-8")
        await conn.did_open(target)
        await conn.did_change(target)
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline and not conn.diagnostics_for(target):
            await asyncio.sleep(0.05)
        diags = conn.diagnostics_for(target)
        assert diags and diags[0]["message"].startswith("fake undefined")
    finally:
        await conn.close()


async def test_manager_start_and_summary(workspace, lsp_config):
    lsp_config({"python": [sys.executable, str(FAKE)]})  # write config
    manager = LspManager(load_lsp_config(), workspace)
    started = await manager.start_all()
    try:
        assert started == ["python"]
        target = workspace / "app.py"
        target.write_text("import os\n", encoding="utf-8")
        summary = await manager.refresh(target)
        assert summary.startswith("[LSP]")
        assert "fake undefined" in summary
    finally:
        await manager.shutdown()
    assert manager.connections == {}


def test_load_lsp_config_missing_or_empty(isolated_home):
    assert load_lsp_config(isolated_home) == {}


async def test_lsp_diagnostics_tool(workspace, lsp_config):
    lsp_config({"python": [sys.executable, str(FAKE)]})  # write config
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=500)
    registry.bind(ctx)
    manager = LspManager(load_lsp_config(), workspace)
    await manager.start_all()
    ctx.session_state["lsp_manager"] = manager
    try:
        target = workspace / "check.py"
        target.write_text("value = 1\n", encoding="utf-8")
        result = await registry.dispatch("lsp_diagnostics", json.dumps({"path": "check.py"}))
        assert not result.is_error
        assert "fake undefined" in result.output
    finally:
        await manager.shutdown()


async def test_lsp_tool_without_server(isolated_home, workspace):
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=300))
    result = await registry.dispatch("lsp_diagnostics", '{"path": "x.py"}')
    assert result.is_error and "No LSP servers configured" in result.output


async def test_write_file_injects_diagnostics(workspace, lsp_config):
    """End-to-end: WRITE tool result gains the [LSP] diagnostics block."""
    lsp_config({"python": [sys.executable, str(FAKE)]})  # write config
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=2000)
    registry.bind(ctx)
    manager = LspManager(load_lsp_config(), workspace)
    await manager.start_all()
    ctx.session_state["lsp_manager"] = manager
    try:
        result = await registry.dispatch(
            "write_file",
            json.dumps({"path": "injected.py", "content": "x = y\n"}),
        )
        assert not result.is_error
        assert "[LSP]" in result.output
        assert "fake undefined" in result.output
    finally:
        await manager.shutdown()
