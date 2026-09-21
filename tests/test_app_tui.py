"""Textual Pilot headless TUI tests: streaming render, permissions, commands."""

from __future__ import annotations

import asyncio
import time

from oaset.config import default_config
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.tui.app import OasetApp
from oaset.tui.widgets.chat import AssistantTurn, NoticeCard, ThinkingBlock
from oaset.tui.widgets.inline import InlinePermission, InlinePicker
from oaset.tui.widgets.tool_card import ToolCard


def make_app(workspace, script):
    cfg = default_config()
    provider = MockProvider(script)
    return OasetApp(cfg=cfg, cwd=workspace, provider=provider, model_id="mock/mock-echo")


async def wait_until(pilot, predicate, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause(0.05)
    return False


def chat_text(app) -> str:
    parts = []
    for turn in app.chat.query(AssistantTurn):
        parts.append(turn.text())
    for notice in app.chat.query(NoticeCard):
        parts.append(notice.raw_text)
    return "\n".join(parts)


async def test_streaming_answer_renders(workspace):
    app = make_app(
        workspace,
        [MockTurn(content_chunks=["Hello ", "streamed ", "world!"], finish_reason="stop")],
    )
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("say hi")
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: "Hello streamed world!" in chat_text(app))
        assert app.conversation.messages[-1].content == "Hello streamed world!"


async def test_read_tool_runs_without_permission(workspace):
    (workspace / "data.txt").write_text("the-data", encoding="utf-8")
    app = make_app(
        workspace,
        [
            MockTurn(content_chunks=["checking"], tool_calls=[MockToolCall("read_file", {"path": "data.txt"})]),
            MockTurn(content_chunks=["found the-data"]),
        ],
    )
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("read it")
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: "found the-data" in chat_text(app))
        cards = list(app.chat.query(ToolCard))
        assert cards and "ReadFile" in cards[-1].title_text
        assert "Used" in cards[-1].title_text or "调用" in cards[-1].title_text


async def test_write_requires_confirmation_modal(workspace):
    app = make_app(
        workspace,
        [
            MockTurn(
                tool_calls=[MockToolCall("write_file", {"path": "made.txt", "content": "created"})]
            ),
            MockTurn(content_chunks=["file created"]),
        ],
    )
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("make a file")
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: list(app.query(InlinePermission)))
        await pilot.press("y")  # allow once
        assert await wait_until(pilot, lambda: "file created" in chat_text(app))
        assert (workspace / "made.txt").read_text(encoding="utf-8") == "created"


async def test_deny_blocks_write(workspace):
    app = make_app(
        workspace,
        [
            MockTurn(
                tool_calls=[MockToolCall("write_file", {"path": "blocked.txt", "content": "x"})]
            ),
            MockTurn(content_chunks=["okay, moving on"]),
        ],
    )
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("make a file")
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: list(app.query(InlinePermission)))
        await pilot.press("n")  # deny
        assert await wait_until(pilot, lambda: "okay, moving on" in chat_text(app))
        assert not (workspace / "blocked.txt").exists()


async def test_slash_help_and_suggest(workspace):
    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        for ch in "/hel":
            await pilot.press(ch)
        assert app.input_area._suggest_visible  # autocomplete popup live
        await pilot.press("enter")  # accept suggestion → "/help "
        await pilot.press("enter")  # submit
        assert await wait_until(pilot, lambda: list(app.chat.query(".chat-card")))


async def test_unknown_command_notices(workspace):
    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        app.submit_text("/definitely-not-a-command")
        assert await wait_until(pilot, lambda: "Unknown command" in chat_text(app) or "未知命令" in chat_text(app))


async def test_esc_interrupts_stream(workspace):
    class SlowProvider(MockProvider):
        async def stream(self, messages, tools):
            async for event in super().stream(messages, tools):
                await asyncio.sleep(0.08)
                yield event

    cfg = default_config()
    provider = SlowProvider([MockTurn(content_chunks=["slow "] * 60, finish_reason="stop")])
    app = OasetApp(cfg=cfg, cwd=workspace, provider=provider, model_id="mock/mock-echo")
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("long task")
        await pilot.press("enter")
        await pilot.pause(0.3)
        await pilot.press("escape")
        assert await wait_until(pilot, lambda: "Interrupted" in chat_text(app) or "已被用户中断" in chat_text(app))
        # conversation remains wire-valid (partial assistant message persisted)
        wire = app.conversation.wire_messages()
        assert wire[-1]["role"] in ("assistant", "user", "tool")


async def test_ctrl_o_folds_tool_cards(workspace):
    (workspace / "f.txt").write_text("x", encoding="utf-8")
    app = make_app(
        workspace,
        [
            MockTurn(tool_calls=[MockToolCall("read_file", {"path": "f.txt"})]),
            MockTurn(content_chunks=["done"]),
        ],
    )
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("read")
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: "done" in chat_text(app))
        cards = list(app.chat.query(ToolCard))
        assert cards
        all_open = all(card.expanded for card in cards)
        await pilot.press("ctrl+o")
        assert all(card.expanded != all_open for card in cards)


async def test_model_picker_switches_model(workspace):
    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        # switching is verify-then-swap: stub the probe so this test covers
        # picker wiring (probe/refusal semantics live in test_tui_semantics)
        async def ok_probe(provider):
            return True, "OK"

        app._probe_provider = ok_probe  # type: ignore[method-assign]
        # the target must be usable: a preset without a credential is refused
        # before any request, which is covered by its own test
        from oaset.credentials import save_credential

        save_credential("openai", "sk-test-openai")
        app.submit_text("/model")
        assert await wait_until(pilot, lambda: list(app.query(InlinePicker)))
        # pick "openai/gpt-4o" (present in default config)
        target = "openai/gpt-4o"
        picker = app.query(InlinePicker).first()
        picker.index = [value for value, _ in picker.options].index(target)
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: app.model_cfg.id == target)
        assert app._kernel.provider is app.provider


async def test_todo_sidebar_toggles(workspace):
    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.press("ctrl+t")
        sidebar = app.query_one("#sidebar")
        assert sidebar.display
        await pilot.press("ctrl+t")
        assert not sidebar.display


async def test_session_persisted_and_view_cleared(workspace):
    app = make_app(workspace, [MockTurn(content_chunks=["answer"])])
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("question one")
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: "answer" in chat_text(app))
        old_id = app.session.meta.session_id
        app.submit_text("/new")
        assert await wait_until(pilot, lambda: app.session.meta.session_id != old_id)
        assert app.conversation.messages == []
        from oaset.session import SessionStore

        store = SessionStore()
        metas = store.list_sessions(cwd=str(workspace))
        assert any(m.message_count >= 2 for m in metas)


async def test_sessions_picker_restores_and_rehydrates(workspace):
    app = make_app(workspace, [MockTurn(content_chunks=["first answer"])])
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("question zero")
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: "first answer" in chat_text(app))
        old_id = app.session.meta.session_id
        app.submit_text("/new")
        assert await wait_until(pilot, lambda: app.session.meta.session_id != old_id)
        assert "first answer" not in chat_text(app)  # fresh view
        app.submit_text("/sessions")
        assert await wait_until(pilot, lambda: list(app.query(InlinePicker)))
        picker = app.query(InlinePicker).first()
        index = [m.session_id for m in app.store.list_sessions(cwd=str(workspace))].index(old_id)
        picker.index = index
        await pilot.press("enter")
        # /sessions now offers an action panel: pick "switch"
        assert await wait_until(pilot, lambda: list(app.query(InlinePicker)))
        action_picker = app.query(InlinePicker).first()
        action_values = [v for v, _ in action_picker.options]
        action_picker.index = action_values.index("switch")
        await pilot.press("enter")
        assert await wait_until(pilot, lambda: "first answer" in chat_text(app))
        assert app.session.meta.session_id == old_id
        assert len(app.conversation.messages) == 2  # user + assistant restored


async def test_ctrl_o_folds_thinking_block(workspace):
    app = make_app(
        workspace,
        [
            MockTurn(
                reasoning_chunks=["pondering ", "the problem "],
                content_chunks=["answer"],
                finish_reason="stop",
            ),
        ],
    )
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("hi")
        await pilot.press("enter")
        blocks = []

        def block_ready():
            blocks[:] = list(app.chat.query(ThinkingBlock))
            return blocks and blocks[0]._buffer and not blocks[0]._streaming

        assert await wait_until(pilot, block_ready)
        block = blocks[0]
        assert block.display
        # Kimi style: the reasoning auto-collapses at turn end; ctrl+o unfolds.
        assert block.collapsed
        await pilot.press("ctrl+o")
        assert not block.collapsed
        await pilot.press("ctrl+o")
        assert block.collapsed


async def test_stream_follows_growing_tail(workspace):
    app = make_app(
        workspace,
        [MockTurn(content_chunks=["line1\n", "line2\n", "line3\n"] * 30, finish_reason="stop")],
    )
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.load_text("long")
        await pilot.press("enter")

        def done_and_pinned():
            turns = list(app.chat.query(AssistantTurn))
            if not turns or not turns[0]._done:
                return False
            return app.chat.follow and app.chat.scroll_y >= app.chat.max_scroll_y - 2

        assert await wait_until(pilot, done_and_pinned, timeout=8.0)
        # simulate a late multi-frame markdown expansion: update the turn's
        # markdown after completion and confirm the view re-pins to the bottom
        turn = app.chat.query(AssistantTurn).first()
        paras = chr(10).join(f"para {i}" for i in range(40))
        turn.markdown.update(turn.text() + chr(10) * 2 + paras + "tail-marker")
        await pilot.pause(0.3)
        assert app.chat.scroll_y >= app.chat.max_scroll_y - 2


async def test_suggest_arrow_keys_move_selection(workspace):
    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.press("/", "m")
        assert app.input_area._suggest_visible
        count = len(app.input_area._suggest_options)
        assert count >= 2
        start = app.input_area._suggest_index
        await pilot.press("down")
        assert app.input_area._suggest_index == (start + 1) % count
        await pilot.press("down")
        await pilot.press("up")
        assert app.input_area._suggest_index == (start + 1) % count


def test_command_registry_matches_handlers():
    from oaset.tui.commands import COMMANDS

    missing = [
        c.name for c in COMMANDS
        if not hasattr(OasetApp, "cmd_" + c.name.replace("-", "_"))
    ]
    assert not missing, f"commands without handlers: {missing}"
