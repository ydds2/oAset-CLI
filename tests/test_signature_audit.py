"""Self-audit pins for the signature batch — the receipts behind the claims."""

from __future__ import annotations

import argparse

import pytest

from oaset.config import default_config
from oaset.local_servers import LOCAL_SERVERS


def _ns(**kwargs):

    defaults = {"session": "", "export": None}
    return argparse.Namespace(**{**defaults, **kwargs})


def test_cmd_evidence_replays_and_exports(isolated_home, capsys):
    """`oaset evidence` was shipped without its own test — the replay/export
    half of the audit chain is a product promise, so it gets pinned here."""
    from oaset.cli import cmd_evidence
    from oaset.i18n import t
    from oaset.session import SessionStore

    store = SessionStore()
    session = store.new_session(isolated_home / "ws", model="m")
    store.append_evidence(session, [
        {"kind": "run", "tool": "run_shell", "summary": "[exit 0] receipt",
         "ok": True, "artifact": "x.txt"},
        {"kind": "diagnostics", "tool": "lsp_diagnostics", "summary": "clean",
         "ok": True},
    ])
    rc = cmd_evidence(_ns(session=session.meta.session_id))
    assert rc == 0
    out = capsys.readouterr().out
    assert "receipt" in out and "run_shell" in out and "x.txt" in out

    out_md = isolated_home / "audit.md"
    rc = cmd_evidence(_ns(session=session.meta.session_id, export=str(out_md)))
    assert rc == 0
    exported = out_md.read_text(encoding="utf-8")
    assert t("evidence_export_header", sid="x") [:6] in exported[:8] or exported.startswith("#")
    assert "lsp_diagnostics" in exported


def test_cmd_evidence_says_so_when_empty(isolated_home, capsys):
    from oaset.cli import cmd_evidence
    from oaset.i18n import t

    rc = cmd_evidence(_ns(session="no-such-session"))
    assert rc == 1
    assert t("evidence_none", sid="no-such-session") in capsys.readouterr().out


def test_offline_check_does_not_claim_models_without_running_servers(
        isolated_home, monkeypatch, capsys):
    """The old listing counted ollama/lmstudio presets as offline-ready merely
    for EXISTING in config — a readiness claim the machine could not back
    when no local server was running."""
    from oaset import cli as climod

    async def empty_discover(*, fetch=None, timeout=0.5):
        return []

    monkeypatch.setattr("oaset.local_servers.discover_local_servers", empty_discover)
    assert climod.cmd_offline_check(default_config()) in (0, 1)
    out = capsys.readouterr().out
    assert "ollama/llama" not in out, "a preset without a running server is not ready"
    assert "mock/mock-echo" in out
    assert "context-headroom" in out and "disk-free" in out


def test_offline_check_lists_local_models_only_when_the_server_answers(
        isolated_home, monkeypatch, capsys):
    from oaset import cli as climod

    async def detecting(*, fetch=None, timeout=0.5):
        vendor, base = LOCAL_SERVERS[0]
        return [{"vendor": vendor, "base_url": base, "models": ["qwen3:8b"]}]

    monkeypatch.setattr("oaset.local_servers.discover_local_servers", detecting)
    climod.cmd_offline_check(default_config())
    out = capsys.readouterr().out
    assert "ollama/llama" in out and "context" in out


def test_code_outline_handles_paths_outside_the_workspace(tmp_path):
    """An approved outside read used to crash the tool: relative_to(cwd)
    raises ValueError for any path outside the workspace."""
    import asyncio
    import json as _json

    from oaset.tools import AutoGate, ToolContext, ToolRegistry

    outside = tmp_path.parent / "elsewhere-src"
    outside.mkdir(exist_ok=True)
    (outside / "m.py").write_text("def gone():\n    pass\n", encoding="utf-8")
    cwd = tmp_path / "project"
    cwd.mkdir(exist_ok=True)

    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=cwd, mode="auto"))
    res = asyncio.run(registry.dispatch(
        "code_outline", _json.dumps({"path": str(outside / "m.py")})))
    assert not res.is_error, res.output
    assert "gone" in res.output


@pytest.mark.asyncio
async def test_panel_gate_times_out_to_deny():
    """Silence is never approval: an unanswered panel approval denies."""
    from oaset.gateway import GatewayService, PanelGate

    service = GatewayService.__new__(GatewayService)
    service.pending_approvals = {}
    service.panel = True
    service.events = []
    service._subscribers = []

    def _publish(event):
        service.events.append(event)

    service._publish = _publish
    gate = PanelGate(service, timeout=0.05)
    decision = await gate.request("run_shell", "exec", "ls -la", preview=None)
    assert decision == "deny"


@pytest.mark.asyncio
async def test_panel_gate_resolves_from_another_thread():
    """The panel decision arrives on an HTTP worker thread; the wake must be
    routed through the awaiting loop (a plain Event.set() raced it)."""
    import asyncio
    import threading

    from oaset.gateway import GatewayService, PanelGate

    service = GatewayService.__new__(GatewayService)
    service.pending_approvals = {}
    service.events = []
    service._subscribers = []
    service._publish = lambda event: None  # not under test here
    gate = PanelGate(service, timeout=5.0)

    def http_thread():
        gate.deliver("approval-1", "allow")

    task = asyncio.ensure_future(
        gate.request("write_file", "write", "write x", preview=None))
    await asyncio.sleep(0.05)
    thread = threading.Thread(target=http_thread)
    thread.start()
    decision = await task
    thread.join()
    assert decision == "allow"


# ------------------------------------------- offline tool list is derived

def test_network_tool_classification_pins_the_boundary():
    """The offline-check trusts Tool.network; the boundary must hold —
    web/browser tools flagged, everything a plane-seat needs unflagged."""
    from oaset.tools import default_tools

    flagged = {t.name for t in default_tools() if t.network}
    assert {"web_fetch", "web_search", "browser_open", "browser_click",
            "browser_type", "browser_screenshot", "browser_close",
            "browser_download", "browser_upload", "browser_observe"} <= flagged
    local = {t.name for t in default_tools() if not t.network}
    assert {"read_file", "write_file", "edit_file", "apply_patch", "glob",
            "grep", "run_shell", "todo_write", "memory", "code_outline",
            "repo_map", "lsp_diagnostics"} <= local
    assert not (flagged & local)


def test_offline_check_tool_list_follows_the_registry(capsys):
    """The list is derived, not snapshotted: local tools appear, network
    tools never do — a renamed or newly added tool cannot drift this."""
    from oaset import cli as climod
    from oaset.config import default_config
    from oaset.tools import default_tools

    climod.cmd_offline_check(default_config())
    out = capsys.readouterr().out
    for name in ("read_file", "apply_patch", "code_outline", "todo_write"):
        assert name in out, f"{name} must be listed as network-free"
    for name in ("web_fetch", "web_search", "browser_open"):
        assert name not in out, f"{name} dials out; it must not be listed"
    # the derivation source is the live registry, so the two agree
    local = {t.name for t in default_tools() if not t.network}
    listed = {w for w in out.split() if w in
              {t.name for t in default_tools()}}
    assert local <= listed
