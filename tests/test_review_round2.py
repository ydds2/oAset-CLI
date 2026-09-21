"""Blind-review round 2 (2026-09-14) regressions.

1. queued messages render once (not twice) and never auto-fire into a NEW
   session after /new
2. clear_view drops archive + selection state (no ghost blocks, no ghost copy)
3. transcript/tool copy includes ARCHIVED blocks past the 600-mount cap
4. /model switch installs the fallback chain on the kernel, not just the app
"""

from __future__ import annotations

import asyncio

from oaset.config import default_config
from oaset.providers import MockProvider, MockTurn
from oaset.tui.app import OasetApp
from oaset.tui.widgets.chat import AssistantTurn, ChatLog, UserMessage
from oaset.tui.widgets.tool_card import ToolCard


def make_app(workspace, turns=None) -> OasetApp:
    cfg = default_config()
    cfg.ui_language = "en"
    return OasetApp(cfg=cfg, cwd=workspace,
                    provider=MockProvider(turns or [MockTurn(content_chunks=["ok"])]),
                    model_id="mock/mock-echo")


def user_message_count(app, text: str) -> int:
    return sum(1 for m in app.chat.query(UserMessage) if m.raw == text)


async def test_queued_message_renders_once(workspace):
    """submit-while-running previews the message; the dequeued send must not
    render it a second time (the old double-render bug)."""
    release = asyncio.Event()

    class SlowProvider(MockProvider):
        async def stream(self, messages, tools):
            yield {"type": "text", "text": "working..."}
            await release.wait()
            yield {"type": "turn_done"}

    app = make_app(workspace, [MockTurn(content_chunks=["done"])])
    app.provider = SlowProvider([MockTurn(content_chunks=["done"])])
    app._kernel.provider = app.provider
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        app.submit_text("first")
        for _ in range(60):
            await pilot.pause(0.05)
            if app._worker_running():
                break
        assert app._worker_running(), "turn must be running to queue"
        app.submit_text("second message")
        await pilot.pause(0.2)
        assert app.input_queue == ["second message"]
        assert user_message_count(app, "second message") == 1, "preview renders once"
        release.set()
        for _ in range(120):
            await pilot.pause(0.1)
            if not app._worker_running() and not app.input_queue:
                break
        await pilot.pause(0.5)
        assert app.input_queue == [], "queue drains after the first turn"
        assert user_message_count(app, "second message") == 1, (
            "the dequeued send must reuse the preview, not render again"
        )
        sent = [m for m in app.conversation.messages
                if m.role == "user" and "second message" in str(m.content)]
        assert len(sent) == 1, "the message was SENT exactly once"


async def test_new_session_drops_queue_instead_of_firing_it(workspace):
    """A queued message belongs to the OLD conversation: /new must drop it,
    not auto-submit it into the fresh session."""
    release = asyncio.Event()

    class SlowProvider(MockProvider):
        async def stream(self, messages, tools):
            yield {"type": "text", "text": "working"}
            await release.wait()
            yield {"type": "turn_done"}

    app = make_app(workspace)
    app.provider = SlowProvider([])
    app._kernel.provider = app.provider
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        app.submit_text("first")
        for _ in range(60):
            await pilot.pause(0.05)
            if app._worker_running():
                break
        app.submit_text("queued for the old session")
        await pilot.pause(0.1)
        old_session = app.session.meta.session_id
        release.set()  # let the turn end while /new runs
        await app.cmd_new("")
        await pilot.pause(0.5)
        assert app.session.meta.session_id != old_session
        assert app.input_queue == [], "queue must be dropped on session swap"
        sent = [m for m in app.conversation.messages
                if m.role == "user" and "old session" in str(m.content)]
        assert not sent, "the old session's queued text must NOT reach the new session"


async def test_clear_view_resets_archive_and_selection(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        chat: ChatLog = app.chat
        ghost = UserMessage("ghost from the previous session")
        chat._archived.append(ghost)
        chat._selection = [ghost]
        chat._sel_anchor = ghost
        chat.clear_view()
        await pilot.pause(0.1)
        assert chat._archived == [], "archived blocks must not survive /clear"
        assert chat._selection == [] and chat._sel_anchor is None
        assert chat._archive_hint.display is False
        assert chat.selected_text() == "", "stale selection must not copy cleared text"


async def test_copy_includes_archived_blocks(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        chat: ChatLog = app.chat
        old_turn = AssistantTurn(live=False)
        old_turn.push_content("archived answer body")
        old_turn.finish()
        card = ToolCard("read_file", "path=x")
        card.finish("archived tool output", is_error=False)
        chat._archived.extend([old_turn, card])
        await pilot.pause(0.1)

        transcript = chat.transcript_text()
        assert "archived answer body" in transcript
        assert "archived tool output" in chat.tool_outputs_text()
        assert old_turn in chat.assistant_turns()
        assert app._last_assistant_text() == "archived answer body"


async def test_model_switch_updates_kernel_fallbacks(workspace):
    """/model must swap the kernel's fallback chain too — leaving it stale made
    provider errors fail over to the model the user switched AWAY from."""
    app = make_app(workspace)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        marker = object()
        app._apply_model_switch(app.model_cfg, app.provider, marker)
        assert app._kernel.fallbacks is marker, "kernel fallback chain must be replaced"
