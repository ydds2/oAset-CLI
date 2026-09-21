"""Network egress policy (只收不发): modes, gates, receive-only transports."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from oaset.config import default_config, load_config, save_config
from oaset.network import (
    NetworkBlockedError,
    NetworkPolicy,
    normalize_mode,
)
from oaset.providers import MockProvider, MockTurn
from oaset.providers.openai_compat import OpenAICompatProvider
from oaset.tools import AutoGate, ToolContext, ToolRegistry


def test_policy_matrix():
    local = NetworkPolicy("local_only")
    pull = NetworkPolicy("pull_only")
    full = NetworkPolicy("full")
    assert not local.allows("pull") and not local.allows("model_call") and not local.allows("push")
    assert pull.allows("pull") and pull.allows("model_call")
    assert not pull.allows("push") and not pull.allows("remote_exec")
    assert full.allows("push") and full.allows("remote_exec")


def test_normalize_mode():
    assert normalize_mode("full") == "full"
    assert normalize_mode("bogus") == "pull_only"
    assert normalize_mode(None) == "pull_only"


async def test_local_only_blocks_model_call_before_any_io(workspace):
    # NON-loopback on purpose: local_only deliberately permits loopback model
    # servers (Ollama / LM Studio), so only a remote endpoint must be refused
    # client-side before a socket is ever opened.
    provider = OpenAICompatProvider(
        "deepseek/x", api_key="k", base_url="https://api.deepseek.example/v1",
        policy=NetworkPolicy("local_only"),
    )
    with pytest.raises(NetworkBlockedError):
        async for _ in provider.stream([{"role": "user", "content": "hi"}], None):
            pass


async def test_web_fetch_blocked_in_local_only(workspace):
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=300, network_mode="local_only")
    registry.bind(ctx)
    result = await registry.dispatch("web_fetch", json.dumps({"url": "https://example.com"}))
    assert result.is_error
    assert "local_only" in result.output or "pull_only" in result.output


async def test_remote_backend_blocked_in_pull_only(workspace):
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=300, network_mode="pull_only")
    registry.bind(ctx)
    ctx.session_state["shell_config"] = {"backend": "modal", "modal_container": "app"}
    result = await registry.dispatch("run_shell", json.dumps({"command": "echo hi"}))
    assert result.is_error and "只收不发" in result.output


async def test_local_backend_runs_in_pull_only(workspace):
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=300, network_mode="pull_only")
    registry.bind(ctx)
    ctx.session_state["shell_config"] = {"backend": "local"}
    result = await registry.dispatch("run_shell", json.dumps({"command": "echo local-ok"}))
    assert not result.is_error and "local-ok" in result.output


def test_default_config_enables_web_fetch(isolated_home):
    """web_fetch is on by default; users opt out via disabled_tools."""
    cfg = default_config()
    assert "web_fetch" not in cfg.disabled_tools
    registry = ToolRegistry(gate=AutoGate())
    registry.set_toggles(enabled=cfg.enabled_tools or None, disabled=cfg.disabled_tools)
    names = [s["function"]["name"] for s in registry.schemas()]
    assert "web_fetch" in names and "web_search" in names and "read_file" in names


def test_config_network_mode_roundtrip(isolated_home):
    cfg = default_config()
    cfg.network_mode = "local_only"
    save_config(cfg)
    assert load_config().network_mode == "local_only"


def test_build_provider_passes_policy(isolated_home, monkeypatch):
    cfg = default_config()
    cfg.network_mode = "local_only"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    from oaset.config import build_provider

    provider = build_provider(cfg, cfg.models["deepseek/deepseek-chat"])
    assert provider._policy is not None and provider._policy.mode == "local_only"


async def test_transports_receive_only_by_default(workspace):
    """Telegram 默认 allow_send=False：收（处理消息）而不发（不回平台）。"""
    from oaset.gateway import GatewayService
    from oaset.transports import TelegramTransport

    sent: list[dict] = []

    class FakeApi:
        polls = 0

        async def __call__(self, method, payload=None):
            if method == "getUpdates":
                FakeApi.polls += 1
                if FakeApi.polls == 1:
                    return [{"update_id": 1,
                             "message": {"chat": {"id": 7}, "text": "hello"}}]
                return []
            if method == "sendMessage":
                sent.append(payload)
                return {}
            return {}

    provider = MockProvider([MockTurn(content_chunks=["reply"])])
    service = GatewayService(default_config(), workspace, provider=provider)

    calls = FakeApi()

    class LocalTransport(TelegramTransport):
        async def _call(self, method, payload=None):
            return await calls(method, payload)

    transport = LocalTransport(service, "tok", allow_send=False, poll_timeout=0)
    task = asyncio.create_task(transport.run_forever())
    try:
        for _ in range(100):
            if transport.events_seen >= 1 and FakeApi.polls >= 3:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)  # grace: a forbidden send would have landed by now
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        await transport.close()
    assert transport.events_seen >= 1   # 收：消息已接收并处理
    assert sent == []                   # 不发：默认策略下回复不外发

