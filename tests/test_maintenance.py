"""体验计划 (Experience Program): budget-capped, opt-in self-maintenance."""

from __future__ import annotations

import hashlib
import json

from oaset.config import default_config
from oaset.maintenance import SelfMaintenance, _extract_plan_json
from oaset.providers import MockProvider, MockTurn
from tests.test_update_center import FakeResponse


class Cfg:
    """Real AppConfig with maintenance flags overridable per test — avoids
    drifting field-by-field stubs against build_registry/shell_config."""

    def __init__(self, allow_apply=False):
        base = default_config()
        base.network_mode = "pull_only"
        base.maintenance.enabled = True
        base.maintenance.allow_apply = allow_apply
        self.__dict__ = vars(base)


PLAN_JSON = ('{"search": "found demo plugin update", "compatibility": ["mock ok"],'
             ' "patch_targets": ["demo"], "self_update": "none",'
             ' "summary": "install demo"}')


def _fake_provider(plan_text: str = PLAN_JSON, total_tokens: int = 1200):
    # turn 1 is consumed by the compatibility probe, turn 2 is the planner
    return MockProvider([
        MockTurn(content_chunks=["OK"], finish_reason="stop",
                 usage={"prompt_tokens": 10, "completion_tokens": 1,
                        "total_tokens": 11}),
        MockTurn(content_chunks=[plan_text], finish_reason="stop",
                 usage={"prompt_tokens": 100, "completion_tokens": 50,
                        "total_tokens": total_tokens}),
    ])


def _demo_catalog(sha: str) -> str:
    return json.dumps({"entries": [{
        "kind": "plugin", "name": "demo", "version": "1.1.0",
        "source_url": "https://cdn.test/demo-1.1.0.py", "sha256": sha,
        "description": "demo patch"}]})


def _seed_cache(mgr, sha: str) -> None:
    import time as _time
    payload = _demo_catalog(sha)
    mgr._write_cache(_parse_entries(payload), _time.time())


def _parse_entries(payload: str):
    from oaset.update import _parse_catalog
    return _parse_catalog(payload)


def _payload_sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


async def test_readonly_mode_reports_plan_never_installs(tmp_path):
    payload = b"print('demo 1.1')"
    sha = _payload_sha(payload)

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_demo_catalog(sha))
        return FakeResponse(payload)

    cfg = Cfg(allow_apply=False)
    cfg.maintenance.enabled = True
    mgr_probe = SelfMaintenance(cfg, tmp_path, fetch=fetch)
    _seed_cache(mgr_probe.manager, sha)

    result = await SelfMaintenance(cfg, tmp_path, provider=_fake_provider(),
                                   fetch=fetch).run()
    assert not result.errors or all("预算" not in e for e in result.errors)
    assert result.plan is not None and result.plan["patch_targets"] == ["demo"]
    assert result.applied == [], "read-only mode must not install"
    assert not (tmp_path / "plugins" / "demo.py").exists()
    assert any(p["name"] == "apply" and "只读" in p["detail"] for p in result.phases)


async def test_allow_apply_installs_hash_gated_targets(tmp_path):
    payload = b"print('demo 1.1')"
    sha = _payload_sha(payload)

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_demo_catalog(sha))
        return FakeResponse(payload)

    cfg = Cfg(allow_apply=True)
    result = await SelfMaintenance(cfg, tmp_path, provider=_fake_provider(),
                                   fetch=fetch, home=tmp_path).run()
    assert "demo" in result.applied
    assert (tmp_path / "plugins" / "demo.py").read_bytes() == payload
    history = json.loads((tmp_path / "update" / "history.jsonl")
                         .read_text(encoding="utf-8").splitlines()[-1])
    assert history["action"] == "maintenance" and history["applied"] == ["demo"]


async def test_unknown_patch_targets_rejected(tmp_path):
    async def fetch(url):
        return FakeResponse(_demo_catalog("0" * 64))

    cfg = Cfg(allow_apply=True)
    result = await SelfMaintenance(
        cfg, tmp_path, provider=_fake_provider(
            '{"patch_targets": ["no-such-plugin"], "summary": "x"}'),
        fetch=fetch, home=tmp_path).run()
    assert result.applied == []
    assert any("未知目标" in e for e in result.errors)


async def test_budget_exhaustion_truncates_turn(tmp_path):
    async def fetch(url):
        return FakeResponse(_demo_catalog("0" * 64))

    cfg = Cfg(allow_apply=False)
    # provider reports 5000 tokens; budget = 1 → turn must be cut off
    result = await SelfMaintenance(cfg, tmp_path, provider=_fake_provider(total_tokens=5000),
                                   fetch=fetch, home=tmp_path).run(budget_tokens=1)
    assert result.usage.get("budget_exhausted") is True
    assert any("预算耗尽" in e for e in result.errors)
    assert result.applied == []


async def test_unparseable_plan_recorded_as_error(tmp_path):
    async def fetch(url):
        return FakeResponse(_demo_catalog("0" * 64))

    cfg = Cfg(allow_apply=False)
    result = await SelfMaintenance(cfg, tmp_path,
                                   provider=_fake_provider("sorry, no json here"),
                                   fetch=fetch).run()
    assert result.plan is None
    assert any("JSON" in e for e in result.errors)


def test_extract_plan_json_handles_fences():
    fenced = "```json\n{\"patch_targets\": [\"a\"]}\n```"
    assert _extract_plan_json(fenced) == {"patch_targets": ["a"]}
    assert _extract_plan_json("no json") is None


def test_maintenance_config_roundtrip(tmp_path, monkeypatch):
    import oaset.config as cfgmod
    from oaset.config import load_config, save_config

    fake = tmp_path / "config.toml"
    monkeypatch.setattr(cfgmod, "config_path", lambda: fake)
    cfg = load_config()
    cfg.maintenance.enabled = True
    cfg.maintenance.budget_tokens = 4096
    cfg.maintenance.allow_apply = True
    save_config(cfg)
    reloaded = load_config()
    assert reloaded.maintenance.enabled is True
    assert reloaded.maintenance.budget_tokens == 4096
    assert reloaded.maintenance.allow_apply is True


def test_maintenance_defaults_safe():
    from oaset.config import default_config

    cfg = default_config()
    assert cfg.maintenance.enabled is False      # opt-in only
    assert cfg.maintenance.allow_apply is False  # read-only by default
    assert cfg.maintenance.budget_tokens > 0


# ------------------------------------------------- core self_update (MAINT-02)

_CORE_PLAN = ('{"search": "new core release", "compatibility": ["mock ok"],'
              ' "patch_targets": ["demo"], "self_update": "core",'
              ' "summary": "install demo + core"}')


def _combined_catalog(sha_demo: str, sha_core: str) -> str:
    return json.dumps({"entries": [
        {"kind": "plugin", "name": "demo", "version": "1.1.0",
         "source_url": "https://cdn.test/demo-1.1.0.py", "sha256": sha_demo},
        {"kind": "core", "name": "oaset-cli", "version": "999.0.0",
         "source_url": "https://cdn.test/oaset-999.0.0.zip", "sha256": sha_core},
    ]})


def _make_live_pkg(tmp_path):
    pkg = tmp_path / "live" / "oaset"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.10.0"\n', encoding="utf-8")
    return pkg


async def test_self_update_core_applies_transactionally(tmp_path):
    """MAINT-02: `self_update: "core"` joins the SAME audited apply batch via
    the transactional core path (hash check, backup, atomic replace), and the
    result flags restart_required instead of silently swapping live code."""
    import io
    import zipfile

    from tests.test_update_center import FakeResponse

    demo_payload = b"print('demo 1.1')"
    demo_sha = _payload_sha(demo_payload)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("oaset/__init__.py", '__version__ = "999.0.0"\n')
    core_payload = buffer.getvalue()
    core_sha = _payload_sha(core_payload)
    pkg = _make_live_pkg(tmp_path)

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_combined_catalog(demo_sha, core_sha))
        if url.endswith("oaset-999.0.0.zip"):
            return FakeResponse(core_payload)
        return FakeResponse(demo_payload)

    cfg = Cfg(allow_apply=True)
    result = await SelfMaintenance(
        cfg, tmp_path, provider=_fake_provider(_CORE_PLAN),
        fetch=fetch, home=tmp_path, package_dir=pkg).run()

    assert result.self_update == "core"
    assert result.self_update_applied and result.restart_required
    assert "oaset-cli" in result.applied and "demo" in result.applied
    assert (pkg / "__init__.py").read_text(encoding="utf-8") == '__version__ = "999.0.0"\n'
    backups = list((tmp_path / "update" / "core-backup").glob("*"))
    assert len(backups) == 1
    history = json.loads((tmp_path / "update" / "history.jsonl")
                         .read_text(encoding="utf-8").splitlines()[-1])
    assert history["action"] == "maintenance"
    assert history["self_update_applied"] is True
    assert history["restart_required"] is True
    rendered = result.render()
    assert "需要重启" in rendered and "rollback" in rendered


async def test_self_update_core_without_core_entry_reports_error(tmp_path):
    """A core recommendation with no core entry in the catalog is a structured
    error, never a silent no-op or a guessed pip command."""
    from tests.test_update_center import FakeResponse

    demo_payload = b"print('demo 1.1')"
    sha = _payload_sha(demo_payload)

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_demo_catalog(sha))
        return FakeResponse(demo_payload)

    cfg = Cfg(allow_apply=True)
    result = await SelfMaintenance(
        cfg, tmp_path, provider=_fake_provider(_CORE_PLAN),
        fetch=fetch, home=tmp_path).run()

    assert result.self_update_applied is False
    assert result.restart_required is False
    assert any("没有 core 条目" in e for e in result.errors)
    assert "demo" in result.applied  # the plugin patch still installed


async def test_self_update_core_recommendation_reported_in_readonly(tmp_path):
    """Read-only mode surfaces the core recommendation but installs nothing —
    the report path must not lose the recommendation it used to drop."""
    import io
    import zipfile

    from tests.test_update_center import FakeResponse

    demo_payload = b"print('demo 1.1')"
    demo_sha = _payload_sha(demo_payload)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("oaset/__init__.py", '__version__ = "999.0.0"\n')
    core_sha = _payload_sha(buffer.getvalue())

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_combined_catalog(demo_sha, core_sha))
        return FakeResponse(demo_payload)

    cfg = Cfg(allow_apply=False)
    result = await SelfMaintenance(
        cfg, tmp_path, provider=_fake_provider(_CORE_PLAN),
        fetch=fetch, home=tmp_path).run()

    assert result.self_update == "core"
    assert result.applied == []
    assert result.self_update_applied is False
    assert any(p["name"] == "apply" and "只读" in p["detail"] for p in result.phases)
