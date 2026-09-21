"""Plugins, Telegram transport, singularity backend, batch runner, style details."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from oaset.agent.prompts import build_system_prompt
from oaset.batch import run_batch
from oaset.config import default_config, load_config, save_config
from oaset.memory import write_memory
from oaset.plugins import load_plugins
from oaset.providers import MockProvider, MockTurn
from oaset.tools import AutoGate, ToolContext, ToolRegistry
from oaset.tools.shell import backend_command_line
from oaset.transports import TelegramTransport
from oaset.tui.widgets.status_bar import fmt_k

# -------------------------------------------------------------------- plugins


def _plugin_source(tool_name: str = "plugin_echo") -> str:
    return f'''
from oaset.tools.base import Tool, ToolResult

class PluginEcho(Tool):
    name = "{tool_name}"
    description = "Echo text from a plugin"
    permission = "read"
    required = ["text"]
    parameters = {{"text": {{"type": "string"}}}}

    async def run(self, args, ctx):
        return ToolResult(f"plugin says: {{args.get('text', '')}}")

def register(api):
    api.add_tool(PluginEcho())
    def cmd_hello(app, args):
        app.chat.add_notice("hello from plugin")
    api.add_command("plughello", "Plugin hello", cmd_hello)
'''


def test_plugins_register_tools_and_commands(isolated_home, workspace):
    plugins_dir = isolated_home / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "echo.py").write_text(_plugin_source(), encoding="utf-8")
    (plugins_dir / "broken.py").write_text("raise RuntimeError('boom')", encoding="utf-8")
    (plugins_dir / "noregister.py").write_text("x = 1", encoding="utf-8")

    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=300)
    registry.bind(ctx)
    extra_commands: dict = {}
    logs: list[str] = []
    loaded = load_plugins(workspace, registry, extra_commands, logs.append)

    assert "echo" in loaded and "broken" not in loaded and "noregister" not in loaded
    assert "plugin_echo" in registry.tools
    # command dispatch through the plugin command table
    from oaset.tui import commands

    table = commands.command_table(extra_commands)
    assert any(name == "plughello" for name, _ in table)


async def test_plugin_tool_dispatch(isolated_home, workspace):
    plugins_dir = isolated_home / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "echo.py").write_text(_plugin_source("plugin_echo2"), encoding="utf-8")
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=300)
    registry.bind(ctx)
    load_plugins(workspace, registry, {}, lambda t: None)
    result = await registry.dispatch("plugin_echo2", json.dumps({"text": "hi"}))
    assert "plugin says: hi" in result.output


# ------------------------------------------------------- telegram transport


class _FakeTelegram(BaseHTTPRequestHandler):
    sent: list[dict] = []
    polls = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/getUpdates"):
            _FakeTelegram.polls += 1
            if _FakeTelegram.polls == 1:
                payload = {"ok": True, "result": [{
                    "update_id": 11,
                    "message": {"chat": {"id": 42}, "text": "hello via telegram"},
                }]}
            else:
                payload = {"ok": True, "result": []}
        elif self.path.endswith("/sendMessage"):
            _FakeTelegram.sent.append(body)
            payload = {"ok": True, "result": {}}
        else:
            payload = {"ok": False, "description": "unknown"}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_telegram_transport_roundtrip(workspace):
    cfg = default_config()
    provider = MockProvider([MockTurn(content_chunks="telegram reply")])
    from oaset.gateway import GatewayService

    service = GatewayService(cfg, workspace, provider=provider)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeTelegram)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    async def run():
        transport = TelegramTransport(service, "TESTTOKEN", api_base=f"http://127.0.0.1:{port}", poll_timeout=0, allow_send=True)
        task = asyncio.create_task(transport.run_forever())
        for _ in range(100):
            if _FakeTelegram.sent:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await transport.close()

    asyncio.run(run())
    server.shutdown()
    server.server_close()
    assert _FakeTelegram.sent, "bot should have replied via sendMessage"
    assert _FakeTelegram.sent[0]["chat_id"] == 42
    assert "telegram reply" in _FakeTelegram.sent[0]["text"]


# ------------------------------------------------------------ shell backends


def test_singularity_backend():
    argv = backend_command_line("run tests", {"backend": "singularity", "singularity_image": "ci.sif"})
    assert argv == ["singularity", "exec", "ci.sif", "bash", "-c", "run tests"]
    with pytest.raises(ValueError):
        backend_command_line("x", {"backend": "singularity", "singularity_image": ""})


# --------------------------------------------------------------------- batch


async def test_batch_runner_writes_results(isolated_home, workspace, tmp_path, monkeypatch):
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(
        '{"id": "t1", "prompt": "first"}\n'
        "\n"
        '{"id": "t2", "prompt": "second"}\n',
        encoding="utf-8",
    )
    out = tmp_path / "runs"
    cfg = default_config()

    async def fake_execute(cfg_, cwd_, prompt, **kwargs):
        return f"reply:{prompt}"

    monkeypatch.setattr("oaset.agent.runner.execute_prompt", fake_execute)
    results = await run_batch(cfg, workspace, tasks, out)
    assert [r["id"] for r in results] == ["t1", "t2"]
    saved = json.loads((out / "t1.json").read_text(encoding="utf-8"))
    assert saved["reply"] == "reply:first" and saved["error"] is None
    assert (out / "t2.json").exists()


# --------------------------------------------------------- style exactness


def test_fmt_k_matches_kimi_format():
    assert fmt_k(447) == "447"
    assert fmt_k(262144) == "262.1k"
    assert fmt_k(0) == "0"


def test_thinking_block_streaming_header():
    from oaset.i18n import t
    from oaset.tui.widgets.chat import ThinkingBlock

    block = ThinkingBlock()
    block.push("reasoning bit")
    rendered = str(block.render())
    # Streaming shows two lines: status + the newest reasoning line.
    assert t("thinking") in rendered
    assert rendered.count("reasoning bit") == 1
    block.finish()
    rendered = str(block.render())
    # Folded: a one-line summary (elapsed · size); ctrl+o expands the text.
    assert "reasoning bit" not in rendered and "1" in rendered
    block.toggle()
    rendered = str(block.render())
    assert "reasoning bit" in rendered


async def test_status_bar_idle_and_busy_text(workspace):
    from oaset.i18n import set_language
    from oaset.tui.widgets.status_bar import StatusBar

    set_language("en")  # this test asserts the English hint strings
    bar = StatusBar()  # constructed inside a loop: set_interval needs one
    bar.set_model("kimi-for-coding", "kimi")
    bar.set_cwd("/Users/x/kimi-code")
    bar.set_branch("main")
    bar.set_max_context(262144)
    bar.set_tokens(0)
    bar.set_busy(False)
    # idle + low usage: the tip owns the right side; the token meter stays off
    assert ("[dim]" in bar.right1_text
            or "/yolo: toggle yolo | esc: interrupt" in bar.right1_text)
    assert "context" not in bar.right1_text
    assert not bar.right2_text
    bar.set_busy(True)
    assert "interrupt" in bar.right1_text or "中断" in bar.right1_text
    assert "working" in bar.left_text
    assert "262.1k" in bar.right1_text or "262.1k" in bar.left_text


def test_yolo_and_mode_in_config(isolated_home):
    cfg = default_config()
    cfg.permission_mode = "auto"
    save_config(cfg)
    assert load_config().permission_mode == "auto"


def test_learning_loop_nudge_in_prompt(workspace, isolated_home):
    write_memory("global", "fact one")
    prompt = build_system_prompt(workspace)
    assert "Learning loop" in prompt
    assert "memory" in prompt and "skill_create" in prompt
    assert "Memory (from previous sessions)" in prompt
    assert "fact one" in prompt
