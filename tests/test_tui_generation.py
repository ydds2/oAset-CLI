"""TUI-03 regressions: worker / session / reload generation consistency.

The review listed this as "risk statically visible, race not yet reproduced in
a run". These tests reproduce the concrete races with a controllable provider:

  * a late event from a superseded turn painted into the live view;
  * a late persistence write landing in the session the user just switched to;
  * an old worker's `finally` clearing a newer turn's busy state;
  * `/reload` closing the provider client a live turn was streaming through;
  * `/reload-mcp` yanking tools out from under an in-flight turn.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from oaset.config import default_config
from oaset.providers import MockProvider, MockTurn
from oaset.providers.base import ContentDelta
from oaset.tui.app import OasetApp


@pytest.fixture(autouse=True)
def isolated_config_path(tmp_path, monkeypatch):
    import oaset.config as cfgmod

    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "config.toml")


class GatedProvider:
    """Streams one chunk, then blocks until released — a turn that stays live."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.aclose_calls = 0

    async def stream(self, messages, tools=None, **kwargs):
        self.started.set()
        yield ContentDelta("partial ")
        await self.release.wait()
        yield ContentDelta("done")

    async def aclose(self) -> None:
        self.aclose_calls += 1


def make_app(workspace, provider=None):
    cfg = default_config()
    cfg.ui_language = "en"
    return OasetApp(cfg=cfg, cwd=workspace,
                    provider=provider or MockProvider(
                        [MockTurn(content_chunks=["ok"], finish_reason="stop")]),
                    model_id="mock/mock-echo")


async def _start_live_turn(app, pilot, text="do the thing"):
    """Submit a prompt and wait until the turn is actually up and streaming."""
    app.submit_text(text)
    for _ in range(100):
        await pilot.pause(0.05)
        if app._turn.active:
            break
    assert app._turn.active, "the turn never started"
    return app._turn.generation, app.session


# ------------------------------------------------------------------ generation


def test_turn_controller_tracks_generations():
    from oaset.tui.app import TurnController

    controller = TurnController()
    assert controller.generation == 0
    assert controller.is_stale(1)

    marker = object()
    gen = controller.begin(marker, marker, None)
    assert gen == 1
    assert not controller.is_stale(gen)
    assert controller.session is marker

    bumped = controller.bump()
    assert bumped == 2
    assert controller.is_stale(gen), "the replaced session invalidates the old turn"
    assert controller.session is None


async def test_events_from_a_superseded_turn_are_dropped(workspace):
    """A late delta/tool event must not paint into the live view."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        gen = app._turn.generation
        turn = app.chat.start_assistant()
        cards: dict = {}

        stale = gen - 1  # as if the session had moved on
        assert app._turn.is_stale(stale)
        app._handle_event({"type": "assistant_delta", "text": "GHOST"}, turn, cards)
        # the guard lives in the emit closure: reproduce it exactly
        emitted: list[dict] = []

        def emit(event):
            if app._turn.is_stale(stale):
                return
            emitted.append(event)

        emit({"type": "assistant_delta", "text": "GHOST"})
        assert emitted == [], "a stale turn must not emit anything"


async def test_late_persistence_lands_on_the_old_session(workspace):
    """A callback bound to the running turn keeps writing to ITS session."""
    provider = GatedProvider()
    app = make_app(workspace, provider=provider)
    async with app.run_test(size=(120, 40)) as pilot:
        gen, old_session = await _start_live_turn(app, pilot)
        old_id = old_session.meta.session_id
        # what _run_agent bound for this turn
        persist = app._kernel.on_message
        assert persist is not None

        await app.cmd_new("")  # user switches sessions mid-turn (now drains)
        await pilot.pause(0.2)
        new_id = app.session.meta.session_id
        assert new_id != old_id
        assert app._turn.is_stale(gen)

        from oaset.agent.messages import Message

        outcome = persist(Message(role="assistant", content="late write"))
        if inspect.isawaitable(outcome):
            await outcome  # persistence now runs on the ordered IO thread (P0-4)
        await pilot.pause(0.1)

        old = app.store.load(old_id)
        new = app.store.load(new_id)
        assert any((m.content or "") == "late write" for m in old.conversation.messages), \
            "the late write belongs to the session the turn started in"
        assert not any((m.content or "") == "late write" for m in new.conversation.messages), \
            "the new session must stay clean"


async def test_old_worker_cannot_clear_a_newer_turns_busy_state(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        gen = app._turn.generation
        app._turn.bump()  # a newer turn / session took over
        app.status_bar.set_busy(True)
        app._set_working(True)
        app._finish_turn(gen, app.conversation)  # the old worker settling
        assert app.status_bar._busy is True, "an old worker must not clear the spinner"
        assert app._working_visible is True


async def test_current_turn_retires_its_own_busy_state(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        gen = app._turn.begin(app.session, app.conversation, None)
        app.status_bar.set_busy(True)
        app._finish_turn(gen, app.conversation)
        assert app.status_bar._busy is False
        assert app._working_visible is False
        assert app._turn.worker is None


async def test_stream_sink_drops_stale_chunks(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        from oaset.tui.widgets.tool_card import ToolCard

        card = app.chat.add_tool_card("demo", "summary")
        app.active_card = card
        gen = app._turn.generation
        app._turn.bump()
        app._stream_sink(gen, "GHOST")
        assert "GHOST" not in getattr(card, "_stream_buffer", "") + str(card.render())
        app._stream_sink(app._turn.generation, "LIVE")
        assert isinstance(card, ToolCard)


# -------------------------------------------------------------- one turn rule


async def test_only_one_turn_runs_and_a_second_prompt_is_queued(workspace):
    provider = GatedProvider()
    app = make_app(workspace, provider=provider)
    async with app.run_test(size=(120, 40)) as pilot:
        gen, _session = await _start_live_turn(app, pilot, "first")
        app.submit_text("second")
        # poll, don't sleep: under a loaded machine the submit takes longer
        # than any fixed pause assumes, and both queue asserts went
        # load-sensitive
        for _ in range(50):
            await pilot.pause(0.1)
            if app.input_queue == ["second"]:
                break
        assert app._turn.generation == gen, "a second prompt must not start a new turn"
        # P1-6: the prompt QUEUES for its own turn — it is not injected into
        # the running one (Ctrl+S remains the mid-stream steer path).
        assert app.input_queue == ["second"]
        assert not app._kernel.injections, "the user queue must not leak into the steer queue"
        from oaset.tui.widgets.chat import NoticeCard

        assert any(n.code == "" and ("queued" in n.raw_text.lower() or "排队" in n.raw_text)
                   for n in app.chat.query(NoticeCard))

        # Ending the live turn drains the queue: the prompt runs as its OWN
        # turn. Poll until it LANDS — a fixed 0.5s was the load-flake.
        provider.release.set()

        def _second_landed() -> bool:
            return (not app.input_queue) and any(
                m.role == "user" and (m.content or "") == "second"
                for m in app.conversation.messages)

        for _ in range(80):  # ≤8s: drain + a whole new turn under load
            await pilot.pause(0.1)
            if _second_landed():
                break
        assert app.input_queue == [], "the queued prompt must auto-send after the turn"
        assert _second_landed(), \
            "the queued prompt must land as a real user message"


async def test_drain_turn_cancels_and_waits(workspace):
    provider = GatedProvider()
    app = make_app(workspace, provider=provider)
    async with app.run_test(size=(120, 40)) as pilot:
        await _start_live_turn(app, pilot)
        assert app._worker_running()
        drained = await app.drain_turn(timeout=3.0)
        assert drained is True
        assert not app._worker_running()


async def test_drain_turn_is_a_no_op_without_a_turn(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        assert await app.drain_turn() is True


# --------------------------------------------------------------- reload safety


async def test_reload_defers_the_provider_close_while_streaming(workspace):
    """/reload must not close the client a live turn is streaming through."""
    provider = GatedProvider()
    app = make_app(workspace, provider=provider)
    async with app.run_test(size=(120, 40)) as pilot:
        await _start_live_turn(app, pilot)
        app.cmd_reload("")
        await pilot.pause(0.3)
        assert app._deferred_provider_close is not None, "the close must be parked"
        assert provider.aclose_calls == 0, "the live client was closed mid-turn"
        assert app.provider is not provider, "the new chain must already be installed"
        from oaset.tui.widgets.chat import NoticeCard

        assert any(n.code == "reload.deferred_close" for n in app.chat.query(NoticeCard))


async def test_reload_closes_immediately_when_idle(workspace):
    provider = GatedProvider()
    app = make_app(workspace, provider=provider)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        assert not app._turn.active
        app.cmd_reload("")
        for _ in range(40):
            await pilot.pause(0.05)
            if provider.aclose_calls:
                break
        assert provider.aclose_calls == 1, "an idle reload must release the old client"
        assert app._deferred_provider_close is None


async def test_repeated_reload_defers_both_closes_in_order(workspace):
    provider = GatedProvider()
    app = make_app(workspace, provider=provider)
    async with app.run_test(size=(120, 40)) as pilot:
        await _start_live_turn(app, pilot)
        app.cmd_reload("")
        await pilot.pause(0.2)
        first = app.provider
        app.cmd_reload("")
        await pilot.pause(0.2)
        closer = app._deferred_provider_close
        assert closer is not None
        await closer()  # simulate the turn finishing and releasing both
        assert provider.aclose_calls == 1
        assert first is not provider


async def test_reload_failure_keeps_the_old_runtime(workspace, monkeypatch):
    """A provider that cannot be rebuilt must leave the live one untouched."""
    provider = GatedProvider()
    app = make_app(workspace, provider=provider)

    def boom(*_args, **_kwargs):
        raise RuntimeError("no provider for this model")

    monkeypatch.setattr("oaset.config.build_provider_chain", boom)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.cmd_reload("")
        await pilot.pause(0.2)
        assert app.provider is provider, "the working provider chain must survive"
        assert app._kernel.provider is provider
        from oaset.tui.widgets.chat import NoticeCard

        assert any(n.code == "reload.provider_failed" for n in app.chat.query(NoticeCard))
        assert app.status_bar.error_text == ""  # a warn is not an error


async def test_reload_mcp_drains_a_live_turn_before_restarting(workspace):
    """MCP restart cancels + waits for the turn instead of racing it."""
    provider = GatedProvider()
    app = make_app(workspace, provider=provider)
    async with app.run_test(size=(120, 40)) as pilot:
        await _start_live_turn(app, pilot)
        manager = app.mcp
        app.cmd_reload_mcp("")
        for _ in range(60):
            await pilot.pause(0.05)
            if app.mcp is not manager:
                break
        assert not app._worker_running(), "the turn must be fully drained first"
        assert app.mcp is not manager, "MCP restarts once the turn is gone"
        from oaset.tui.widgets.chat import NoticeCard

        assert any(n.code == "mcp.reload_draining" for n in app.chat.query(NoticeCard)), \
            "the drain must be announced, not silent"


async def test_reload_mcp_proceeds_when_idle(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        before = app.mcp
        app.cmd_reload_mcp("")
        for _ in range(40):
            await pilot.pause(0.05)
            if app.mcp is not before:
                break
        assert app.mcp is not before, "an idle /reload-mcp must rebuild the manager"
