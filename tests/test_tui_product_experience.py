"""TUI product-experience pass: transcript fidelity, structured fan-out,
help hierarchy, session/checkpoint panels, and the settings surface.
"""

from __future__ import annotations

import time

import pytest

from oaset.agent.messages import Conversation, Message, ToolCallReq
from oaset.config import default_config, load_config
from oaset.providers import MockProvider, MockTurn
from oaset.tui.app import OasetApp
from oaset.tui.commands import commands_by_category
from oaset.tui.widgets.chat import AssistantTurn
from oaset.tui.widgets.inline import help_text
from oaset.tui.widgets.tool_card import ToolCard


@pytest.fixture(autouse=True)
def isolated_config_path(tmp_path, monkeypatch):
    import oaset.config as cfgmod

    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "config.toml")


def make_app(workspace):
    cfg = default_config()
    cfg.ui_language = "en"
    return OasetApp(cfg=cfg, cwd=workspace,
                    provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                    model_id="mock/mock-echo")


# ------------------------------------------------------- transcript fidelity


def _conversation_with_tool_call() -> Conversation:
    conversation = Conversation(system_prompt="s")
    conversation.messages.append(Message(role="user", content="read it"))
    conversation.messages.append(Message(
        role="assistant", content=None,
        tool_calls=[ToolCallReq(id="c1", name="read_file",
                                arguments='{"path": "src/app.py"}')]))
    conversation.messages.append(Message(
        role="tool", content="line1\nline2\nline3\n" * 40, tool_call_id="c1"))
    return conversation


async def test_rehydrate_uses_real_tool_name_and_full_output(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        app.chat.rehydrate(_conversation_with_tool_call())
        await pilot.pause(0.1)
        cards = list(app.chat.query(ToolCard))
        assert cards, "restored transcript must render a tool card"
        card = cards[0]
        assert card.tool_name == "read_file"          # not the anonymous "tool"
        assert "app.py" in card.title_text            # argument summary kept
        assert len(card.raw_output) > 100             # full result, not 100 chars
        assert card.raw_output.count("\n") > 10


async def test_rehydrate_keeps_tools_between_assistant_blocks(workspace):
    """Restored history must match a live turn: text, then the tool card, then
    the post-tool answer — not dump every tool at the end."""
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        conversation = Conversation(system_prompt="s")
        conversation.messages.extend([
            Message(role="user", content="look"),
            Message(role="assistant", content="I'll read it",
                    tool_calls=[ToolCallReq(id="c1", name="read_file",
                                            arguments='{"path": "a.txt"}')]),
            Message(role="tool", content="file body", tool_call_id="c1"),
            Message(role="assistant", content="here is the file"),
        ])
        app.chat.rehydrate(conversation)
        await pilot.pause(0.1)
        kinds = [type(w).__name__ for w in app.chat.selectable_blocks()
                 if type(w).__name__ in ("UserMessage", "AssistantTurn", "ToolCard")]
        assert kinds == ["UserMessage", "AssistantTurn", "ToolCard", "AssistantTurn"], kinds
        turns = list(app.chat.query(AssistantTurn))
        assert "I'll read it" in turns[0].text()
        assert "here is the file" in turns[-1].text()


def test_send_history_survives_restart(isolated_home):
    from oaset.tui.widgets import input as input_mod

    input_mod._save_send_history(["first prompt", "second prompt"])
    restored = input_mod._load_send_history()
    assert restored[-2:] == ["first prompt", "second prompt"]


async def test_rehydrate_marks_error_results(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.1)
        conversation = Conversation(system_prompt="s")
        conversation.messages.append(Message(
            role="assistant", content=None,
            tool_calls=[ToolCallReq(id="e1", name="run_shell", arguments='{"command": "x"}')]))
        conversation.messages.append(Message(
            role="tool", content="boom", tool_call_id="e1", error=True))
        app.chat.rehydrate(conversation)
        await pilot.pause(0.1)
        card = list(app.chat.query(ToolCard))[0]
        assert card.tool_name == "run_shell"
        assert card.has_class("tool-error")


# ------------------------------------------------- structured fan-out report


async def test_parallel_report_carries_per_task_metadata(workspace):
    from oaset.tools import AutoGate, ToolContext, ToolRegistry
    from oaset.tools.parallel_subagent import ParallelTaskTool

    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=4000)
    registry.bind(ctx)

    async def runner(agent, prompt, meta=None):
        if meta is not None:
            meta.update(status="ok", elapsed=1.5, tool_calls=2,
                        usage={"prompt_tokens": 30, "completion_tokens": 10,
                               "total_tokens": 40},
                        agent=agent.name)
        return f"report for {prompt}"

    ctx.session_state["subagent_runner"] = runner
    result = await ParallelTaskTool().run({"tasks": [
        {"description": "A", "prompt": "do A"},
        {"description": "B", "prompt": "do B"},
    ]}, ctx)
    assert not result.is_error
    assert "report for do A" in result.output and "report for do B" in result.output
    assert "1.5s" in result.output          # elapsed per task
    assert "2 tool calls" in result.output  # tool-call count per task
    assert "↑30 ↓10 tok" in result.output  # in/out tokens per task
    assert result.output.count("general") >= 2  # agent named per task


async def test_parallel_report_shows_failures_structurally(workspace):
    from oaset.tools import AutoGate, ToolContext, ToolRegistry
    from oaset.tools.parallel_subagent import ParallelTaskTool

    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=4000)
    registry.bind(ctx)

    async def runner(agent, prompt, meta=None):
        if "boom" in prompt:
            raise RuntimeError("provider exploded")
        return f"ok {prompt}"

    ctx.session_state["subagent_runner"] = runner
    result = await ParallelTaskTool().run({"tasks": [
        {"description": "good", "prompt": "fine"},
        {"description": "bad", "prompt": "boom"},
    ]}, ctx)
    assert "FAILED: RuntimeError: provider exploded" in result.output
    assert "ok fine" in result.output


async def test_single_task_result_carries_metadata_footer(workspace):
    from oaset.tools import AutoGate, ToolContext, ToolRegistry
    from oaset.tools.subagent import TaskTool

    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=4000)
    registry.bind(ctx)
    ctx.session_state["agents"] = {}

    async def runner(agent, prompt, meta=None):
        if meta is not None:
            meta.update(status="ok", elapsed=0.4, tool_calls=1,
                        usage={"prompt_tokens": 5, "completion_tokens": 5,
                               "total_tokens": 10})
        return "the answer"

    ctx.session_state["subagent_runner"] = runner
    result = await TaskTool().run({"agent": "general", "prompt": "x"}, ctx)
    assert "the answer" in result.output
    assert "0.4s" in result.output and "↑5 ↓5 tok" in result.output


def test_two_argument_runner_still_supported(workspace):
    """User-supplied 2-arg runners must keep working (public injection point)."""
    import asyncio

    from oaset.agent.subagent_runner import call_subagent_runner
    from oaset.agents import BUILTIN_GENERAL

    async def legacy(agent, prompt):
        return f"legacy {prompt}"

    meta: dict = {}
    out = asyncio.run(call_subagent_runner(legacy, BUILTIN_GENERAL, "hi", meta))
    assert out == "legacy hi"
    assert meta == {}  # untouched, no crash


# ------------------------------------------------------------ help hierarchy


def test_help_groups_commands_by_category_with_shortcuts(workspace):
    from oaset.tui.commands import category_label

    text = help_text()
    assert "/login" in text and "/skills" in text
    assert "/selfmaint" not in text and "/market" not in text
    assert "/help all" in text.lower() or "help all" in text.lower()
    lowered = text.lower()
    assert "ctrl+k" in lowered and "ctrl+shift+c/v" in lowered
    assert "ctrl+o" not in lowered and "  f2" not in lowered
    assert "✎" in text or "⚠" in text
    full = help_text(all_commands=True)
    assert "ctrl+o" in full.lower() and "f2" in full.lower()
    groups = [name for name, _ in commands_by_category()]
    assert groups
    for category in groups:
        assert category_label(category) in full


def test_slash_suggest_empty_prefix_is_primary_only():
    from oaset.tui.commands import PRIMARY_COMMANDS, suggest

    empty = [name for name, _ in suggest("")]
    assert "login" in empty and "skills" in empty
    assert "selfmaint" not in empty and "market" not in empty
    assert set(empty) <= PRIMARY_COMMANDS
    typed = [name for name, _ in suggest("mark")]
    assert "market" in typed


def test_every_category_has_a_label_key():
    from oaset.i18n import CATALOG
    from oaset.tui.commands import CATEGORY_KEYS, CATEGORY_ORDER

    for category in CATEGORY_ORDER:
        key = CATEGORY_KEYS[category]
        assert key in CATALOG and CATALOG[key].get("en") and CATALOG[key].get("zh")


# --------------------------------------------------- session / checkpoint UI


async def test_undo_lists_checkpoints_and_restores_the_chosen_one(workspace):
    app = make_app(workspace)
    target = workspace / "edited.txt"
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        target.write_text("v1", encoding="utf-8")
        app.store.checkpoint(app.session, {str(target): "v1"})
        target.write_text("v2", encoding="utf-8")
        app.store.checkpoint(app.session, {str(target): "v2"})
        target.write_text("v3", encoding="utf-8")

        rows = app.store.list_checkpoints(app.session)
        assert [r["n"] for r in rows] == [2, 1]  # newest first

        app.submit_text("/undo")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not list(app.query("InlinePicker")):
            await pilot.pause(0.05)
        picker = app.query("InlinePicker").first()
        values = [v for v, _ in picker.options]
        picker.index = values.index("1")  # restore the OLDER checkpoint
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert target.read_text(encoding="utf-8") == "v1"


async def test_undo_without_checkpoints_says_so(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/undo")
        await pilot.pause(0.2)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "checkpoint" in rendered.lower()


async def test_sessions_offer_switch_show_and_delete(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        first = app.session.meta.session_id
        app.submit_text("/new")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and app.session.meta.session_id == first:
            await pilot.pause(0.05)

        app.submit_text("/sessions")
        while time.monotonic() < deadline and not list(app.query("InlinePicker")):
            await pilot.pause(0.05)
        picker = app.query("InlinePicker").first()
        values = [v for v, _ in picker.options]
        assert first in values
        picker.index = values.index(first)
        await pilot.press("enter")
        await pilot.pause(0.2)
        action = app.query("InlinePicker").first()
        actions = [v for v, _ in action.options]
        assert {"switch", "show", "delete"} <= set(actions)

        action.index = actions.index("delete")
        await pilot.press("enter")
        await pilot.pause(0.2)
        confirm = app.query("InlinePicker").first()
        confirm.index = [v for v, _ in confirm.options].index("yes")
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert first not in [m.session_id for m in app.store.list_sessions(
            cwd=str(workspace))]


async def test_fork_can_pick_a_source_session(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/fork")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not list(app.query("InlinePicker")):
            await pilot.pause(0.05)
        picker = app.query("InlinePicker").first()
        values = [v for v, _ in picker.options]
        assert "__current__" in values
        picker.index = values.index("__current__")
        await pilot.press("enter")
        await pilot.pause(0.3)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "fork" in rendered.lower() or "派生" in rendered


# --------------------------------------------------------- settings surface


async def test_settings_changes_network_mode_and_persists(workspace, tmp_path):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/settings network")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not list(app.query("InlinePicker")):
            await pilot.pause(0.05)
        picker = app.query("InlinePicker").first()
        values = [v for v, _ in picker.options]
        assert {"local_only", "pull_only", "full"} <= set(values)
        picker.index = values.index("full")
        await pilot.press("enter")
        # Egress policy is a security boundary: the change is gated, so the
        # permission panel now stands between the picker and the write.
        assert await _answer_permission(pilot, app, "y"), \
            "switching to full egress must ask for permission"
        await pilot.pause(0.3)
        assert app.cfg.network_mode == "full"


async def test_settings_network_change_can_be_refused(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        before = app.cfg.network_mode
        app.submit_text("/settings network")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not list(app.query("InlinePicker")):
            await pilot.pause(0.05)
        picker = app.query("InlinePicker").first()
        values = [v for v, _ in picker.options]
        picker.index = values.index("full" if before != "full" else "local_only")
        await pilot.press("enter")
        assert await _answer_permission(pilot, app, "n")
        await pilot.pause(0.3)
        assert app.cfg.network_mode == before, "a refused change must not apply"


async def _answer_permission(pilot, app, key: str, timeout: float = 3.0) -> bool:
    """Answer an inline permission panel if one is waiting.

    Returns whether a panel was found, so a caller can assert the gate actually
    fired rather than silently passing when it did not.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if list(app.query("InlinePermission")):
            await pilot.press(key)
            await pilot.pause(0.2)
            return True
        await pilot.pause(0.05)
    return False


async def test_settings_edits_a_hook_and_persists(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/settings hooks")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not list(app.query("InlinePicker")):
            await pilot.pause(0.05)
        picker = app.query("InlinePicker").first()
        values = [v for v, _ in picker.options]
        assert "on_turn_end" in values
        picker.index = values.index("on_turn_end")
        await pilot.press("enter")
        await pilot.pause(0.2)
        from conftest import wait_for_inline_input

        field = await wait_for_inline_input(pilot, app)
        field.value = 'echo "done"'
        await pilot.press("enter")
        # A hook runs a shell command on every matching event, so persisting one
        # is confirmed as dangerous code execution.
        assert await _answer_permission(pilot, app, "y"), \
            "writing a lifecycle hook must ask for permission"
        await pilot.pause(0.3)
        assert app.cfg.hooks.get("on_turn_end") == 'echo "done"'
        saved = load_config()
        assert saved.hooks.get("on_turn_end") == 'echo "done"'


async def test_settings_rule_view_and_menu(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.cmd_settings("rules")
        await pilot.pause(0.2)
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "permission" in rendered.lower() or "权限" in rendered


def _screen(app) -> str:
    strips = app.screen._compositor.render_strips()
    return "\n".join("".join(seg.text for seg in strip) for strip in strips)


async def test_idle_screen_keeps_prompt_glyph_and_leaves_the_tip_room(workspace):
    """Claude/Kimi density: ❯ stays on the input row; idle footer is a tip,
    not a token-percentage that crowds it out."""
    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        text = _screen(app)
        assert "❯" in text
        assert "Type a question" in text or "直接提问" in text
        assert "context:" not in text.lower()
        assert "❯" in str(app.query_one("#prompt-glyph").render())
        app.input_area.load_text("hello world")
        await pilot.pause(0.1)
        drafted = _screen(app)
        assert "❯" in drafted and "hello world" in drafted


async def test_user_message_uses_prompt_glyph_not_assistant_star(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app.chat.add_user("look at this")
        await pilot.pause(0.1)
        text = _screen(app)
        assert "› look at this" in text
        assert "❯ look at this" not in text
        assert "✦ look at this" not in text


async def test_help_first_screen_shows_daily_commands(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        app.submit_text("/help")
        await pilot.pause(0.3)
        text = _screen(app)
        assert "Daily commands" in text or "常用命令" in text
        assert "/login" in text or "/new" in text or "/sessions" in text
        assert "Ctrl+O" not in text and "F2" not in text


async def test_welcome_greets_by_time_and_counts_sessions(workspace):
    """The page has a pulse: a time-of-day greeting (one of five) and a
    session ordinal. The greeting is bold and prefixes the title."""
    from oaset.tui.widgets.chat import WelcomePanel

    app = make_app(workspace)
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause(0.3)
        text = str(app.chat.query(WelcomePanel).first().render())
        greetings = ("早上好", "中午好", "下午好", "晚上好", "夜深了",
                     "Good morning", "Good afternoon", "Good evening",
                     "Late night")
        assert any(g in text for g in greetings), "a greeting must render"
        assert "次会话" in text or "session #" in text, "ordinal must render"


def test_welcome_fun_pool_has_twelve_lines():
    """Six personality lines felt thin; the pool is twelve now."""
    from oaset.i18n import CATALOG

    present = [k for k in CATALOG if k.startswith("welcome_fun_")]
    assert len(present) == 12, f"fun pool is {len(present)}, want 12"
