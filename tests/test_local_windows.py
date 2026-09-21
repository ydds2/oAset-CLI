"""v0.10: local-only hardening + MCP server mode + Git integration + encoding."""

from __future__ import annotations

import json
import subprocess

from oaset.mcp_server import McpServerCore, build_server_registry
from oaset.tools import AutoGate, ToolContext, ToolRegistry
from oaset.tools.git_tool import GitStatusTool


def _reg(tmp_path, mode="auto"):
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=tmp_path, mode=mode, output_limit=1000,
                              network_mode="pull_only"))
    return registry


# ---------------------------------------------------------------- MCP server


def test_mcp_server_core_initialize_and_tools(tmp_path):
    registry = _reg(tmp_path)
    core = McpServerCore(registry, expose="readonly")
    resp = core.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert resp["result"]["serverInfo"]["name"] == "oaset"
    listing = core.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in listing["result"]["tools"]]
    assert "read_file" in names and "run_shell" not in names  # readonly exposure
    pong = core.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert pong["result"] == {}
    unknown = core.handle({"jsonrpc": "9", "id": 4, "method": "nope"})
    assert unknown["error"]["code"] == -32603


def test_mcp_server_tool_call_executes(tmp_path):
    (tmp_path / "hello.txt").write_text("mcp-content", encoding="utf-8")
    registry = _reg(tmp_path)
    core = McpServerCore(registry, expose="readonly")
    resp = core.handle({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"path": "hello.txt"}},
    })
    assert resp["result"]["content"][0]["text"].endswith("mcp-content")


def test_mcp_server_all_tools_exposes_write(tmp_path):
    registry = build_server_registry(tmp_path, expose="all")
    core = McpServerCore(registry, expose="all")
    listing = core.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in listing["result"]["tools"]]
    assert "run_shell" in names and "write_file" in names


# ------------------------------------------------------------- git integration


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                   check=not args or args[0] != "init")


def test_git_status_tool_on_real_repo(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, capture_output=True)
    (tmp_path / "tracked.txt").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "init"], cwd=tmp_path, capture_output=True)
    (tmp_path / "tracked.txt").write_text("v2", encoding="utf-8")

    tool = GitStatusTool()
    import asyncio

    result = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        tool.run({}, ToolContext(cwd=tmp_path, mode="auto", output_limit=500))
    )
    assert "main" in result.output or "master" in result.output
    assert "unstaged changes (1)" in result.output and "tracked.txt" in result.output


def test_git_status_non_repo_graceful(tmp_path):
    tool = GitStatusTool()
    import asyncio

    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(
        tool.run({}, ToolContext(cwd=tmp_path, mode="auto", output_limit=500))
    )
    loop.close()
    assert "Not a git repository" in result.output


def test_prompt_includes_git_block(tmp_path):

    from oaset.agent.prompts import build_system_prompt

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, capture_output=True)
    prompt = build_system_prompt(tmp_path)
    assert "## Git" in prompt  # unborn HEAD is reported too


# ------------------------------------------------------------- encoding


def test_gbk_roundtrip_via_tools(tmp_path):
    import asyncio

    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=tmp_path, mode="auto", output_limit=500)
    registry.bind(ctx)
    gbk_file = tmp_path / "gbk.txt"
    gbk_file.write_bytes("中文注释".encode("gbk"))

    async def flow():
        read_result = await registry.dispatch(
            "read_file", json.dumps({"path": "gbk.txt"}))
        assert "中文注释" in read_result.output
        edit_result = await registry.dispatch(
            "edit_file", json.dumps({
                "path": "gbk.txt", "old_text": "中文注释", "new_text": "改好的注释"}))
        assert not edit_result.is_error
        raw = gbk_file.read_bytes()
        assert "改好的注释".encode("gbk") == raw  # stays GBK, not utf-8

    asyncio.run(flow())


def test_write_preserves_crlf(tmp_path):
    import asyncio

    from oaset.tools.fs import WriteFileTool

    target = tmp_path / "crlf.txt"
    target.write_bytes(b"line1\r\nline2\r\n")

    async def run():
        ctx = ToolContext(cwd=tmp_path, mode="auto", output_limit=500)
        await WriteFileTool().run({"path": str(target), "content": "new\r\nlines\r\n"},
                                  ctx)

    asyncio.run(run())
    assert b"\r\n" in target.read_bytes()


# ------------------------------------------------------------------ json -p


def test_output_format_json_available():
    from oaset.cli import build_parser

    args = build_parser().parse_args(["--output-format", "json", "-p", "hi", "--mock"])
    assert args.output_format == "json" and args.mock
