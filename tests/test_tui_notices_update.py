"""TUI-04 regressions: structured notices, a status bar that keeps errors
visible, and an update center whose write actions are separately confirmed and
gated.

Pre-fix behaviour these lock down:
  * failures were free-form strings with no code, no recovery hint and no
    single funnel — a reviewer could not tell which subsystem failed;
  * `StatusBar` replaced the previous content on every refresh, so an error
    was overwritten by the next rotating tip within seconds;
  * `/update` only exposed check / refresh / list / plan: nothing in the TUI
    could install or roll back, so no approval could ever be attached to it.
"""

from __future__ import annotations

import json

import pytest

from oaset.config import default_config
from oaset.providers import MockProvider, MockTurn
from oaset.tui.app import OasetApp
from oaset.tui.notice import UiNotice
from oaset.tui.widgets.chat import NoticeCard
from oaset.tui.widgets.palette import InlineCommandConfirm


@pytest.fixture(autouse=True)
def isolated_config_path(tmp_path, monkeypatch):
    import oaset.config as cfgmod

    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "config.toml")


def make_app(workspace):
    cfg = default_config()
    cfg.ui_language = "en"
    return OasetApp(cfg=cfg, cwd=workspace, provider=MockProvider(
        [MockTurn(content_chunks=["ok"], finish_reason="stop")]), model_id="mock/mock-echo")


# ----------------------------------------------------------------- UiNotice


def test_notice_renders_code_and_hint():
    notice = UiNotice("could not write config", "error", code="config.write_failed",
                      hint="Check the file permissions.")
    assert notice.headline() == "[config.write_failed] could not write config"
    assert "→ Check the file permissions." in notice.render()
    assert notice.is_error


def test_notice_from_update_error_carries_code_and_hint():
    from oaset.update import UpdateError

    exc = UpdateError("apply_busy", "另一个更新事务正在进行", "稍后重试")
    notice = UiNotice.from_exception(exc, source="update")
    assert notice.code == "apply_busy"
    assert notice.hint == "稍后重试"
    assert notice.level == "error"


def test_notice_from_plain_exception_names_the_source_and_type():
    notice = UiNotice.from_exception(KeyError("no-such-model"), source="model")
    assert notice.code == "model.KeyError"
    assert "KeyError" in notice.text


def test_unknown_level_is_normalised():
    assert UiNotice("x", "catastrophe").level == "info"


def test_notice_of_coerces_everything():
    assert UiNotice.of("plain").text == "plain"
    assert UiNotice.of(ValueError("boom")).level == "error"
    original = UiNotice("keep", "warn")
    assert UiNotice.of(original) is original


# ------------------------------------------------------------- notice funnel


async def test_notify_error_pins_the_status_bar(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.notify_error("cannot reach provider", code="provider.network", source="provider")
        await pilot.pause(0.1)
        assert app.status_bar.error_text == "[provider.network] cannot reach provider"
        assert "provider.network" in app.status_bar.right1_text
        cards = [n for n in app.chat.query(NoticeCard) if n.level == "error"]
        assert cards and cards[-1].code == "provider.network"


async def test_status_bar_error_survives_refreshes_and_tips(workspace):
    """The rotating tip carousel must not overwrite an unresolved error."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.notify_error("boom", code="test.boom")
        for _ in range(30):  # simulate many refresh/tip cycles
            app.status_bar._next_tip()
            app.status_bar.set_tokens(1234)
            app.status_bar.set_busy(True)
            app.status_bar.set_busy(False)
        await pilot.pause(0.1)
        assert app.status_bar.error_text == "[test.boom] boom"
        assert "✗" in app.status_bar.right1_text


async def test_clear_error_restores_the_tips(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.notify_error("boom", code="test.boom")
        app.status_bar.clear_error()
        await pilot.pause(0.1)
        assert app.status_bar.error_text == ""
        assert "✗" not in app.status_bar.right1_text


async def test_a_new_prompt_retires_the_pinned_error(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.notify_error("boom", code="test.boom")
        app.submit_text("a fresh prompt")
        await pilot.pause(0.3)
        assert app.status_bar.error_text == ""


async def test_turn_failure_is_reported_with_a_code(workspace):
    app = make_app(workspace)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover - kernel.chat is an async generator

    app._kernel.chat = boom  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("hello")
        await pilot.pause(0.5)
        assert app.status_bar.error_text.startswith("[agent.RuntimeError]")
        assert "provider exploded" in app.status_bar.error_text


# ------------------------------------------------------- update center actions


async def test_update_picker_offers_apply_and_rollback(workspace):
    from oaset.tui.widgets.inline import InlinePicker

    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/update")
        import time as _time

        deadline = _time.monotonic() + 4.0
        while _time.monotonic() < deadline and not app.query(InlinePicker):
            await pilot.pause(0.05)
        offered = {v for v, _ in app.query_one(InlinePicker).options}
        assert {"check", "list", "plan", "refresh", "apply", "rollback", "history"} <= offered
        await pilot.press("q")
        await pilot.pause(0.2)


async def test_update_apply_without_a_plan_reports_and_installs_nothing(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/update apply")
        await pilot.pause(0.6)
        assert not app.query(InlineCommandConfirm), "nothing to confirm without a plan"
        cards = [n for n in app.chat.query(NoticeCard) if n.code == "update.no_targets"]
        assert cards, "an empty plan must say so with a code"


async def test_update_apply_requires_confirmation_then_approval(workspace, monkeypatch):
    """apply must show the command line, then ask the gate; denying both the
    confirmation and the gate leaves the filesystem untouched."""
    from oaset.update import CatalogEntry, UpdateManager, UpdatePlan

    plan = UpdatePlan(targets=[
        CatalogEntry(kind="plugin", name="demo", version="1.0", description="demo")])

    applied: list = []

    async def fake_apply(self, targets=None, dry_run=False):
        applied.append(list(targets or []))
        from oaset.update import UpdateResult
        return UpdateResult(action="apply")

    monkeypatch.setattr(UpdateManager, "plan", lambda self, kind=None: plan)
    monkeypatch.setattr(UpdateManager, "apply", fake_apply)

    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/update apply demo")
        await pilot.pause(0.5)
        confirm = app.query_one(InlineCommandConfirm)
        assert confirm.command_line == "oaset update apply demo"
        await pilot.press("n")  # decline the confirmation
        await pilot.pause(0.4)
        assert applied == [], "declining the confirmation must not install"
        assert not app.query(InlineCommandConfirm)


async def test_update_apply_gate_denial_stops_installation(workspace, monkeypatch):
    from oaset.update import CatalogEntry, UpdateManager, UpdatePlan

    plan = UpdatePlan(targets=[
        CatalogEntry(kind="plugin", name="demo", version="1.0", description="demo")])
    calls: list[list] = []

    async def fake_apply(self, targets=None, dry_run=False):
        calls.append(list(targets or []))
        from oaset.update import UpdateResult
        return UpdateResult(action="apply")

    async def deny(self, tool_name, level, summary, preview=None):
        calls.append([f"gate:{tool_name}:{level}"])
        return "deny"

    monkeypatch.setattr(UpdateManager, "plan", lambda self, kind=None: plan)
    monkeypatch.setattr(UpdateManager, "apply", fake_apply)
    monkeypatch.setattr("oaset.tui.app.UIGate.request", deny)

    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/update apply demo")
        await pilot.pause(0.5)
        await pilot.press("y")  # accept the confirmation page
        await pilot.pause(0.5)
        assert calls == [["gate:update_apply:write"]], calls
        assert not app.query(InlineCommandConfirm)
        cards = [n for n in app.chat.query(NoticeCard) if n.code == "update.apply_denied"]
        assert cards, "a denied apply must be reported with a code"


async def test_update_apply_gate_allow_executes(workspace, monkeypatch):
    from oaset.update import CatalogEntry, UpdateManager, UpdatePlan, UpdateResult

    plan = UpdatePlan(targets=[
        CatalogEntry(kind="plugin", name="demo", version="1.0", description="demo")])
    calls: list[list] = []

    async def fake_apply(self, targets=None, dry_run=False):
        calls.append(list(targets or []))
        result = UpdateResult(action="apply")
        result.notes.append("installed")
        return result

    async def allow(self, tool_name, level, summary, preview=None):
        return "allow"

    monkeypatch.setattr(UpdateManager, "plan", lambda self, kind=None: plan)
    monkeypatch.setattr(UpdateManager, "apply", fake_apply)
    monkeypatch.setattr("oaset.tui.app.UIGate.request", allow)

    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/update apply demo")
        await pilot.pause(0.5)
        await pilot.press("y")
        await pilot.pause(0.5)
        assert calls == [["demo"]], calls
        assert app.status_bar.error_text == ""


async def test_update_history_starts_empty_with_a_message(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/update history")
        await pilot.pause(0.5)
        assert "No update history yet" in " ".join(str(c.render()) for c in app.chat.children)


async def test_update_history_lists_transactions(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        from oaset.utils import oaset_home

        hist = oaset_home() / "update" / "history.jsonl"
        hist.parent.mkdir(parents=True, exist_ok=True)
        hist.write_text(json.dumps({
            "ts": "2026-09-10T12:00:00", "action": "apply", "target": "demo",
            "version": "1.0", "txn": "t1",
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        app.submit_text("/update history")
        await pilot.pause(0.5)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "demo" in rendered and "1.0" in rendered


async def test_update_list_kind_filter_and_detail_view(workspace, monkeypatch):
    """`/update list plugin detail` filters by kind and renders per-entry
    provenance (source/sha256/compat/permissions) in the chat card."""
    from oaset.update import CatalogEntry, UpdateManager, UpdateResult

    seen: dict = {}
    result = UpdateResult(action="list", entries=[
        CatalogEntry(kind="plugin", name="demo", version="1.2.0",
                     source_url="https://cdn.test/demo.py", sha256="ab" * 32,
                     requires_oaset=">=0.9.0", permissions=("fs.write",),
                     published_at="2026-09-01"),
    ], from_cache=True, refreshed_at=1.0)

    def fake_list(self, kind=None):
        seen["kind"] = kind
        return result

    monkeypatch.setattr(UpdateManager, "list_entries", fake_list)

    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/update list plugin detail")
        await pilot.pause(0.5)
        assert seen["kind"] == "plugin"
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "https://cdn.test/demo.py" in rendered
        assert "fs.write" in rendered
        assert "requires_oaset >=0.9.0" in rendered


async def test_update_list_without_detail_keeps_compact_output(workspace, monkeypatch):
    """Plain `/update list` stays one-line-per-entry: provenance stays opt-in."""
    from oaset.update import CatalogEntry, UpdateManager, UpdateResult

    result = UpdateResult(action="list", entries=[
        CatalogEntry(kind="plugin", name="demo", version="1.2.0",
                     source_url="https://cdn.test/demo.py"),
    ], from_cache=True, refreshed_at=1.0)
    monkeypatch.setattr(UpdateManager, "list_entries", lambda self, kind=None: result)

    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/update list")
        await pilot.pause(0.5)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "demo @ 1.2.0" in rendered
        assert "cdn.test" not in rendered



# ------------------------------------------------------------------ status copy


def test_status_labels_are_localised():
    from oaset.i18n import t

    for key in ("status_header", "status_session", "status_model", "status_mode",
                "status_context", "status_tools", "status_mcp", "status_cwd",
                "status_untitled", "status_network_language", "status_tools_detail",
                "status_mcp_detail"):
        assert t(key, lang="en") != key, key
        assert t(key, lang="zh") != key, key


async def test_status_command_uses_labels_not_raw_field_names(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.cmd_status("")
        await pilot.pause(0.1)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "mode" in rendered and "network:" in rendered
        # the old hardcoded layout printed the literal `title:` field name
        assert "title:" not in rendered


def test_every_new_catalog_key_has_both_languages():
    from oaset.i18n import CATALOG

    for key in ("palette_title", "palette_footer", "confirm_title", "confirm_footer",
                "picker_position", "picker_footer", "permission_title", "permission_keys",
                "permission_always", "permission_always_dir", "selfmaint_refused",
                "update_apply_denied", "update_error_hint", "update_history_title",
                "picker_update_apply", "agent_failed_hint", "model_unknown_hint",
                "session_load_hint", "reload_failed_hint", "export_failed_hint",
                "plan_mode_badge", "sidebar_todos", "sidebar_no_todos", "undo_ok",
                "usage_line", "no_subagents", "search_pull_hint", "no_skills",
                "login_banner", "update_rollback_last", "update_rollback_summary"):
        assert CATALOG[key].get("en"), key
        assert CATALOG[key].get("zh"), key
