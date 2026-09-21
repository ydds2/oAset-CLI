"""End-to-end: real OpenAI SDK → examples/mock_server.py over HTTP → AgentLoop.

Proves the full provider protocol path (SSE over the wire) with zero cloud
dependencies: exactly what `oaset -p --mock-server` exercises offline.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from oaset.agent import AgentLoop, Conversation, initial_system_prompt
from oaset.config import ProviderConfig, default_config
from oaset.providers.openai_compat import OpenAICompatProvider
from oaset.tools import AutoGate, ToolContext, ToolRegistry

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "mock_server.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def mock_server():
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(EXAMPLES), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail("mock server did not start")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


async def test_e2e_tool_cycle_over_http(mock_server, tmp_path):
    (tmp_path / "demo.txt").write_text("demo content line", encoding="utf-8")
    cfg = default_config()
    cfg.providers["live-mock"] = ProviderConfig(
        "live-mock", "openai", api_key="mock", base_url=f"{mock_server}/v1"
    )
    cfg.models["live-mock/mock-echo"] = cfg.models["mock/mock-echo"]
    _model_cfg = cfg.models["live-mock/mock-echo"]

    provider = OpenAICompatProvider(
        model_id="live-mock/mock-echo", api_key="mock", base_url=f"{mock_server}/v1"
    )
    system_prompt, _ = initial_system_prompt(tmp_path)
    conversation = Conversation(system_prompt)
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=tmp_path, mode="auto", output_limit=500))
    loop = AgentLoop(provider, registry, conversation, max_iterations=5)

    events: list[dict] = []
    await loop.run("please check demo.txt", events.append)

    assert any(e["type"] == "tool_start" and e["name"] == "read_file" for e in events)
    assert any(e["type"] == "turn_done" for e in events)
    final = events[-1]["content"]
    assert "mock pipeline works" in final
    assert "demo content line" in "\n".join(
        m.content or "" for m in conversation.messages if m.role == "tool"
    )
