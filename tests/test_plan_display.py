"""Plan/progress display: the agent's todo plan pins above the input while it
runs, then unpins when every step is done.

Pins: auto-show on todo_write (unless the user closed the strip), progress
counting in the header, the in-progress row first, the turn-meta plan summary,
the exactly-once completion notice, and auto-hide on completion.
"""

from __future__ import annotations

from oaset.config import default_config
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.tui.app import OasetApp

TODOS = [
    {"content": "read the config", "status": "completed"},
    {"content": "write the fix", "status": "in_progress"},
    {"content": "run the tests", "status": "pending"},
]


def make_app(workspace, script=None):
    provider = MockProvider(script or [])
    return OasetApp(cfg=default_config(), cwd=workspace, provider=provider,
                    model_id="mock/mock-echo")


def _seed_plan(app, todos=TODOS):
    app.registry.ctx.session_state["todos"] = [dict(t) for t in todos]


def _strip_text(app) -> str:
    return "\n".join(str(c.render()) for c in app.side_bar.children)


async def test_todo_write_auto_shows_the_plan(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        assert not app.side_bar.display, "starts hidden"
        _seed_plan(app)
        app._handle_event({"type": "tool_completed", "id": "t1", "name": "todo_write",
                           "result": "ok"}, None, {}, 0.0)
        await pilot.pause(0.2)
        assert app.side_bar.display, "writing a plan must pin it above the input"
        text = _strip_text(app)
        assert "1/3" in text, "the header carries the progress count"
        assert "write the fix" in text


async def test_user_closed_sidebar_stays_closed(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        _seed_plan(app)
        app._refresh_sidebar()
        app.action_toggle_sidebar()  # explicit user close
        assert app._sidebar_pref is False
        app._handle_event({"type": "tool_completed", "id": "t1", "name": "todo_write",
                           "result": "ok"}, None, {}, 0.0)
        await pilot.pause(0.2)
        assert not app.side_bar.display, "the user's close wins over auto-show"


async def test_completed_plan_unpins_the_strip(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        _seed_plan(app)
        app._refresh_sidebar()
        assert app.side_bar.display
        _seed_plan(app, [{**t, "status": "completed"} for t in TODOS])
        app._refresh_sidebar()
        assert not app.side_bar.display, "a finished plan must unpin"


async def test_turn_meta_reports_plan_and_completion_noticed_once(workspace):
    script = [MockTurn(content_chunks=["done"], tool_calls=[
        MockToolCall("todo_write", {"todos": TODOS})])]
    app = make_app(workspace, script)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.input_area.load_text("go")
        await pilot.press("enter")
        import time as _t

        end = _t.monotonic() + 5
        while _t.monotonic() < end:
            turns = list(app.chat.query_assistant_turns()) if hasattr(
                app.chat, "query_assistant_turns") else []
            if turns and turns[-1].meta_text():
                break
            await pilot.pause(0.05)
        _seed_plan(app, [dict(t) for t in TODOS])
        turn = app.chat.start_assistant()
        app._handle_event({"type": "turn_started", "user_text": "x"}, turn, {})
        app._handle_event({"type": "turn_completed", "content": "ok"}, turn, {})
        meta = turn.meta_text()
        assert "计划 1/3" in meta, "the meta line reports plan progress"
        _seed_plan(app, [{**t, "status": "completed"} for t in TODOS])
        app._handle_event({"type": "turn_completed", "content": "ok2"}, turn, {})
        app._handle_event({"type": "turn_completed", "content": "ok3"}, turn, {})
        from oaset.tui.widgets.chat import NoticeCard

        notices = [n for n in app.chat.query(NoticeCard) if "计划完成" in n.raw_text]
        assert len(notices) == 1, "the completion notice must not repeat"


async def test_plan_progress_counts_completed(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert app.plan_progress() == (0, 0)
        _seed_plan(app)
        assert app.plan_progress() == (1, 3)
