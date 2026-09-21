"""MAINT-01 regressions: hard policy guards for the self-maintenance run.

Each test pins one of the guards the review flagged as missing:
  * `enabled=false` did not refuse at the entry point;
  * budget was accepted unchecked;
  * a failed check/plan/compat phase or an exhausted budget still produced an
    applicable plan;
  * `apply=True` could bypass the durable config opt-in and any approval;
  * cancellation left no record of why the run was incomplete.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from oaset.config import default_config
from oaset.maintenance import (
    MAX_BUDGET_TOKENS,
    MaintenanceError,
    SelfMaintenance,
)
from oaset.providers import MockProvider, MockTurn
from oaset.providers.base import ContentDelta
from tests.test_update_center import FakeResponse

PLAN_JSON = ('{"search": "s", "compatibility": ["ok"],'
             ' "patch_targets": ["demo"], "self_update": "none", "summary": "x"}')
PLUGIN_PAYLOAD = b"print('demo 1.1')"


def _cfg(*, enabled=True, allow_apply=False):
    cfg = default_config()
    cfg.network_mode = "pull_only"
    cfg.maintenance.enabled = enabled
    cfg.maintenance.allow_apply = allow_apply
    cfg.maintenance.budget_tokens = 20000
    return cfg


def _sha(payload: bytes = PLUGIN_PAYLOAD) -> str:
    return hashlib.sha256(payload).hexdigest()


def _catalog(sha: str) -> str:
    return json.dumps({"entries": [{
        "kind": "plugin", "name": "demo", "version": "1.1.0",
        "source_url": "https://cdn.test/demo-1.1.0.py", "sha256": sha,
        "description": "demo patch"}]})


def _planning_provider(plan_text: str = PLAN_JSON):
    """Turn 1 = compat probe, turn 2 = the planner."""
    return MockProvider([
        MockTurn(content_chunks=["OK"], finish_reason="stop",
                 usage={"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}),
        MockTurn(content_chunks=[plan_text], finish_reason="stop",
                 usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}),
    ])


def _seed_cache(maint, sha, *, age_seconds=0.0):
    import time as _time

    from oaset.update import _parse_catalog

    maint.manager._write_cache(_parse_catalog(_catalog(sha)), _time.time() - age_seconds)


def _fetch_ok(sha: str):
    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_catalog(sha))
        return FakeResponse(PLUGIN_PAYLOAD)

    return fetch


def _fetch_dead():
    async def fetch(url):
        raise OSError("dns down (injected)")

    return fetch


class _DenyGate:
    def __init__(self, decision="deny", boom=False):
        self.decision = decision
        self.boom = boom
        self.calls: list[tuple] = []

    async def request(self, tool_name, level, summary, preview=None):
        self.calls.append((tool_name, level, summary, preview))
        if self.boom:
            raise RuntimeError("gate unavailable")
        return self.decision


# ----------------------------------------------------------------- entry guards


async def test_disabled_refuses_at_entry_and_writes_nothing(tmp_path):
    cfg = _cfg(enabled=False)
    maint = SelfMaintenance(cfg, tmp_path, provider=_planning_provider(),
                            fetch=_fetch_ok(_sha()), home=tmp_path)
    with pytest.raises(MaintenanceError) as excinfo:
        await maint.run()
    assert excinfo.value.code == "disabled"
    # a refused run must not download, plan, call the model or leave history
    assert not (tmp_path / "update" / "history.jsonl").exists()
    assert not (tmp_path / "plugins").exists()


@pytest.mark.parametrize("budget", [0, -1, -5000, "many", 3.7])
async def test_invalid_budget_refused_before_any_work(tmp_path, budget):
    cfg = _cfg()
    maint = SelfMaintenance(cfg, tmp_path, provider=_planning_provider(),
                            fetch=_fetch_ok(_sha()), home=tmp_path)
    with pytest.raises(MaintenanceError) as excinfo:
        await maint.run(budget_tokens=budget)
    assert excinfo.value.code == "bad_budget"
    assert not (tmp_path / "update" / "history.jsonl").exists()


async def test_budget_above_cap_refused(tmp_path):
    cfg = _cfg()
    maint = SelfMaintenance(cfg, tmp_path, provider=_planning_provider(),
                            fetch=_fetch_ok(_sha()), home=tmp_path)
    with pytest.raises(MaintenanceError) as excinfo:
        await maint.run(budget_tokens=MAX_BUDGET_TOKENS + 1)
    assert excinfo.value.code == "budget_too_large"


async def test_budget_from_config_is_validated_too(tmp_path):
    cfg = _cfg()
    cfg.maintenance.budget_tokens = 0
    maint = SelfMaintenance(cfg, tmp_path, fetch=_fetch_ok(_sha()), home=tmp_path)
    with pytest.raises(MaintenanceError) as excinfo:
        await maint.run()
    assert excinfo.value.code == "bad_budget"


# ------------------------------------------------------------- apply authority


async def test_apply_flag_cannot_bypass_config_optin(tmp_path):
    """apply=True must never turn on a capability config keeps off."""
    cfg = _cfg(allow_apply=False)
    maint = SelfMaintenance(cfg, tmp_path, provider=_planning_provider(),
                            fetch=_fetch_ok(_sha()), home=tmp_path)
    _seed_cache(maint, _sha())
    result = await maint.run(apply=True)
    assert result.requested_apply is True
    assert result.effective_apply is False
    assert result.applied == []
    assert not (tmp_path / "plugins" / "demo.py").exists()
    assert any(p["name"] == "apply" and "allow_apply" in p["detail"] for p in result.phases)


async def test_gate_denial_blocks_install(tmp_path):
    cfg = _cfg(allow_apply=True)
    gate = _DenyGate("deny")
    maint = SelfMaintenance(cfg, tmp_path, provider=_planning_provider(),
                            fetch=_fetch_ok(_sha()), home=tmp_path, gate=gate)
    _seed_cache(maint, _sha())
    result = await maint.run()
    assert result.effective_apply is True
    assert result.applied == []
    assert not (tmp_path / "plugins" / "demo.py").exists()
    assert any(p["name"] == "approve" and not p["ok"] for p in result.phases)
    assert gate.calls and gate.calls[0][1] == "write"  # graded as a write action


async def test_gate_allow_installs(tmp_path):
    cfg = _cfg(allow_apply=True)
    gate = _DenyGate("allow")
    maint = SelfMaintenance(cfg, tmp_path, provider=_planning_provider(),
                            fetch=_fetch_ok(_sha()), home=tmp_path, gate=gate)
    _seed_cache(maint, _sha())
    result = await maint.run()
    assert result.applied == ["demo"]
    assert (tmp_path / "plugins" / "demo.py").read_bytes() == PLUGIN_PAYLOAD


async def test_broken_gate_denies_instead_of_allowing(tmp_path):
    cfg = _cfg(allow_apply=True)
    maint = SelfMaintenance(cfg, tmp_path, provider=_planning_provider(),
                            fetch=_fetch_ok(_sha()), home=tmp_path,
                            gate=_DenyGate(boom=True))
    _seed_cache(maint, _sha())
    result = await maint.run()
    assert result.applied == []
    assert not (tmp_path / "plugins" / "demo.py").exists()
    assert any("审批" in e for e in result.errors)


# ------------------------------------------------------------- phase hard gates


async def test_failed_check_phase_blocks_install(tmp_path):
    """A stale cache with an unreachable catalog fails `check`; the plan the
    model emits afterwards must not reach the installer."""
    cfg = _cfg(allow_apply=True)
    maint = SelfMaintenance(cfg, tmp_path, provider=_planning_provider(),
                            fetch=_fetch_dead(), home=tmp_path)
    _seed_cache(maint, _sha(), age_seconds=10 * 24 * 3600)
    result = await maint.run()
    assert any(p["name"] == "check" and not p["ok"] for p in result.phases)
    assert result.applied == []
    assert not (tmp_path / "plugins" / "demo.py").exists()
    assert any("前置阶段失败" in reason for reason in result.blocked_reasons)


async def test_compat_failure_blocks_install(tmp_path):
    class _FailProvider:
        async def stream(self, messages, tools=None, **kwargs):
            raise RuntimeError("provider refused (injected)")
            yield ContentDelta("")  # pragma: no cover - keeps this an async gen

    cfg = _cfg(allow_apply=True)
    maint = SelfMaintenance(cfg, tmp_path, provider=_FailProvider(),
                            fetch=_fetch_ok(_sha()), home=tmp_path)
    _seed_cache(maint, _sha())
    result = await maint.run()
    assert any(p["name"] == "compat" and not p["ok"] for p in result.phases)
    assert result.applied == []
    assert any("兼容性探测未通过" in reason for reason in result.blocked_reasons)


async def test_budget_exhaustion_blocks_install(tmp_path):
    cfg = _cfg(allow_apply=True)
    maint = SelfMaintenance(
        cfg, tmp_path,
        provider=MockProvider([
            MockTurn(content_chunks=["OK"], finish_reason="stop",
                     usage={"total_tokens": 11}),
            MockTurn(content_chunks=[PLAN_JSON], finish_reason="stop",
                     usage={"total_tokens": 5000}),
        ]),
        fetch=_fetch_ok(_sha()), home=tmp_path)
    _seed_cache(maint, _sha())
    result = await maint.run(budget_tokens=1)
    assert result.usage.get("budget_exhausted") is True
    assert result.applied == []
    assert not (tmp_path / "plugins" / "demo.py").exists()
    assert any("预算" in reason for reason in result.blocked_reasons)
    assert any(p["name"] == "apply" and not p["ok"] for p in result.phases)


async def test_unparseable_plan_blocks_install(tmp_path):
    cfg = _cfg(allow_apply=True)
    maint = SelfMaintenance(cfg, tmp_path, provider=_planning_provider("no json at all"),
                            fetch=_fetch_ok(_sha()), home=tmp_path)
    _seed_cache(maint, _sha())
    result = await maint.run()
    assert result.plan is None
    assert result.applied == []
    assert any("没有可解析的维护计划" in reason for reason in result.blocked_reasons)


# --------------------------------------------------------------------- cancel


async def test_cancellation_stops_turn_records_reason_and_installs_nothing(tmp_path):
    reached = asyncio.Event()

    class _HangingProvider:
        def __init__(self):
            self.calls = 0

        async def stream(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield ContentDelta("OK")
                return
            reached.set()
            await asyncio.sleep(3600)  # pragma: no cover - cancelled first
            yield ContentDelta("")  # pragma: no cover

    cfg = _cfg(allow_apply=True)
    maint = SelfMaintenance(cfg, tmp_path, provider=_HangingProvider(),
                            fetch=_fetch_ok(_sha()), home=tmp_path)
    _seed_cache(maint, _sha())
    task = asyncio.create_task(maint.run())
    await asyncio.wait_for(reached.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not (tmp_path / "plugins" / "demo.py").exists()
    records = [json.loads(line) for line in
               (tmp_path / "update" / "history.jsonl").read_text(encoding="utf-8").splitlines()]
    record = next(r for r in records if r.get("action") == "maintenance")
    assert record["cancelled"] is True
    assert record["applied"] == []
    assert record["enabled"] is True


# -------------------------------------------------------------------- history


async def test_history_records_policy_decisions(tmp_path):
    cfg = _cfg(allow_apply=False)
    maint = SelfMaintenance(cfg, tmp_path, provider=_planning_provider(),
                            fetch=_fetch_ok(_sha()), home=tmp_path)
    _seed_cache(maint, _sha())
    await maint.run()
    record = json.loads((tmp_path / "update" / "history.jsonl")
                        .read_text(encoding="utf-8").splitlines()[-1])
    assert record["action"] == "maintenance"
    assert record["enabled"] is True
    assert record["requested_apply"] is False
    assert record["effective_apply"] is False
    assert record["cancelled"] is False
    assert record["blocked"] == []
    assert record["applied"] == []


async def test_readonly_run_still_reports_the_plan(tmp_path):
    """The hard gates must not turn a legitimate read-only run into a failure."""
    cfg = _cfg(allow_apply=False)
    maint = SelfMaintenance(cfg, tmp_path, provider=_planning_provider(),
                            fetch=_fetch_ok(_sha()), home=tmp_path)
    _seed_cache(maint, _sha())
    result = await maint.run()
    assert result.plan is not None
    assert result.applied == []
    assert result.blocked_reasons == []
    assert not result.errors
