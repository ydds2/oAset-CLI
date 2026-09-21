"""Completeness plan A: plan-mode restore, persist, subagent kernel, local models."""

from __future__ import annotations

import inspect

from oaset.agent.runner import execute_prompt
from oaset.agent.subagent_runner import make_subagent_runner
from oaset.config import default_config
from oaset.host import SessionHost
from oaset.local_models import merge_discovered_models, parse_model_ids, refresh_local_models
from oaset.providers import MockProvider, MockTurn
from oaset.sdk import Oaset
from oaset.session import SessionStore
from oaset.tools.ask_user import ExitPlanModeTool


async def test_host_exit_plan_restores_auto(workspace):
    cfg = default_config()
    cfg.permission_mode = "auto"
    provider = MockProvider([MockTurn(content_chunks=["ok"])])
    async with SessionHost(
        cfg, workspace, provider=provider, model_id="mock/mock-echo",
        persist=False, mcp=False, yolo=True,
    ) as host:
        assert host.mode == "auto"
        host.set_mode("plan")
        assert host.mode == "plan"
        assert host.registry.ctx.mode == "plan"
        restored = host.restore_mode()
        assert restored == "auto"
        assert host.mode == "auto"
        assert host.registry.ctx.mode == "auto"


async def test_exit_plan_mode_tool_restores_previous(workspace):
    cfg = default_config()
    cfg.permission_mode = "auto"
    provider = MockProvider([MockTurn(content_chunks=["ok"])])
    async with SessionHost(
        cfg, workspace, provider=provider, model_id="mock/mock-echo",
        persist=False, mcp=False, yolo=True,
    ) as host:
        host.set_mode("plan")
        result = await ExitPlanModeTool().run({}, host.registry.ctx)
        assert not result.is_error
        assert "auto" in result.output
        assert host.mode == "auto"


async def test_sdk_and_runner_persist_jsonl_by_default(isolated_home, workspace):
    cfg = default_config()
    provider = MockProvider([MockTurn(content_chunks=["sdk-persist"])])
    agent = Oaset(
        config=cfg, cwd=workspace, model="mock/mock-echo",
        provider=provider, tools=False, mcp=False,
    )
    assert agent.host.persist is True
    await agent.ask("hello persist")
    loaded = agent.host.store.load(agent.host.session.meta.session_id)
    assert loaded is not None
    texts = [m.content for m in loaded.conversation.messages]
    assert "hello persist" in texts
    await agent.aclose()

    reply = await execute_prompt(
        cfg, workspace, "runner persist",
        provider=MockProvider([MockTurn(content_chunks=["runner-ok"])]),
        with_tools=False, with_mcp=False, persist=True,
    )
    assert reply == "runner-ok"
    listed = SessionStore().list_sessions(cwd=str(workspace))
    assert listed, "headless runner should write a JSONL session"


async def test_sdk_can_still_be_ephemeral(isolated_home, workspace):
    agent = Oaset(
        cwd=workspace, model="mock/mock-echo",
        provider=MockProvider([MockTurn(content_chunks=["tmp"])]),
        tools=False, mcp=False, persist=False,
    )
    await agent.ask("ephemeral")
    assert agent.host.session.file is None
    await agent.aclose()


def test_subagent_runner_constructs_kernel_not_loop():
    source = inspect.getsource(make_subagent_runner)
    assert "AgentKernel" in source
    assert "AgentLoop(" not in source


def test_subagent_runner_drops_the_verify_carry():
    """The verify-gate carry is parent-scoped: a sub-agent's gate must not
    nag it to verify files the MAIN thread left unverified."""
    source = inspect.getsource(make_subagent_runner)
    assert "verify_pending_paths" in source


async def test_local_model_discovery_merges_tags():
    cfg = default_config()
    payload = {"data": [{"id": "llama3.1:8b"}, {"id": "qwen2.5-coder"}]}
    names = parse_model_ids(payload)
    assert names == ["llama3.1:8b", "qwen2.5-coder"]
    added = merge_discovered_models(cfg, "ollama", names)
    assert "ollama/llama3.1:8b" in cfg.models
    assert "ollama/qwen2.5-coder" in cfg.models
    assert set(added) >= {"ollama/llama3.1:8b", "ollama/qwen2.5-coder"}

    async def fake_fetch(url, headers):
        assert url.endswith("/models")
        return payload

    cfg2 = default_config()
    more = await refresh_local_models(cfg2, fetch=fake_fetch)
    assert "ollama/llama3.1:8b" in more or "ollama/llama3.1:8b" in cfg2.models


async def test_local_discovery_silence_on_dead_server():
    cfg = default_config()
    before = set(cfg.models)

    async def boom(url, headers):
        raise ConnectionError("offline")

    added = await refresh_local_models(cfg, fetch=boom)
    assert added == []
    assert set(cfg.models) == before


def test_selfmaintenance_turn_goes_through_the_kernel_door():
    """maintenance used to build an AgentLoop directly and speak the loop's
    internal event dialect; every surface must use the one kernel door."""
    from oaset import maintenance

    source = inspect.getsource(maintenance.SelfMaintenance._model_turn)
    assert "AgentKernel" in source
    assert "AgentLoop(" not in source
