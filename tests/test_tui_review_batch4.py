"""Regressions for the full-audit round.

* The turn meta line joins with exactly one separator.
* The per-turn usage write is awaited, and a failure is reported.
"""

from __future__ import annotations

import pytest


def _meta_turn():
    """An AssistantTurn with a real app behind it (widgets need one)."""
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp

    app = OasetApp(cfg=default_config(), cwd=".", provider=MockProvider([]),
                   model_id="mock/mock-echo")
    return app


# ------------------------------------------------------------- meta line


async def test_meta_line_uses_a_single_separator(workspace):
    """`add_meta` prepended a space while callers passed " · item", so the meta
    line rendered `… · 1.2s  · ≈$0.01` with a double space."""
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp
    from oaset.tui.widgets.chat import AssistantTurn

    app = OasetApp(cfg=default_config(), cwd=workspace, provider=MockProvider([]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        turn = AssistantTurn(live=False)
        app.chat.mount(turn)
        await pilot.pause(0.1)
        turn.finish(usage={"prompt_tokens": 10, "completion_tokens": 5,
                           "total_tokens": 15},
                    elapsed=1.23, model="mock/mock-echo")
        turn.add_meta(" · ≈$0.01")
        turn.add_meta(" · plan 2/3")

        text = turn.meta_text()
    assert "  " not in text, f"double space in meta line: {text!r}"
    assert text.count(" · ") == 3, text
    assert text.endswith("plan 2/3"), text


async def test_meta_line_handles_items_without_their_own_separator(workspace):
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp
    from oaset.tui.widgets.chat import AssistantTurn

    app = OasetApp(cfg=default_config(), cwd=workspace, provider=MockProvider([]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        turn = AssistantTurn(live=False)
        app.chat.mount(turn)
        await pilot.pause(0.1)
        turn.finish(model="m", elapsed=0.5)
        turn.add_meta("≈$0.01")
        assert turn.meta_text() == "m · 0.5s · ≈$0.01"

        before = turn.meta_text()
        turn.add_meta("   ")
        turn.add_meta(" · ")
        assert turn.meta_text() == before, "an empty item must not add a separator"


# ------------------------------------------------------- usage write


async def test_turn_completed_parks_the_usage_write_for_the_turn(workspace):
    """`run_io` returns an executor Future. Called without being awaited it
    still enqueues the write, but nothing observes the result — a failure was
    reported nowhere, and the "usage is on disk" ordering was not established."""
    from oaset.config import default_config
    from oaset.events import normalize_loop_event
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp
    from oaset.tui.turn_view import handle_turn_event

    app = OasetApp(cfg=default_config(), cwd=workspace, provider=MockProvider([]),
                   model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app._pending_io = None
        event = normalize_loop_event(
            {"type": "turn_completed",
             "usage": {"prompt_tokens": 3, "completion_tokens": 4,
                       "total_tokens": 7}},
            session_id="s", sequence=1)
        turn = app.chat.start_assistant()
        handle_turn_event(app, event, turn, {}, 0.0)

        assert app._pending_io is not None, "the usage write must be awaitable"
        assert hasattr(app._pending_io, "__await__")
        await app._pending_io


async def test_usage_write_is_awaited_by_the_turn(workspace, isolated_home):
    """A failing usage append must be reported instead of vanishing."""
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn
    from oaset.tui.app import OasetApp

    provider = MockProvider([MockTurn(
        content_chunks=["hi"],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})])
    app = OasetApp(cfg=default_config(), cwd=workspace, provider=provider,
                   model_id="mock/mock-echo")

    def failing_append(*a, **k):
        raise OSError("disk full")

    reported: list[str] = []
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.4)
        app.store.append_usage = failing_append  # type: ignore[method-assign]
        real_notify = app.notify_error

        def spy(exc_or_text, **kw):
            reported.append(str(kw.get("code") or ""))
            return real_notify(exc_or_text, **kw)

        app.notify_error = spy  # type: ignore[method-assign]
        app.submit_text("hello")
        for _ in range(40):
            await pilot.pause(0.1)
            if not app._worker_running():
                break
        await pilot.pause(0.3)
        assert app._pending_io is None, "the turn must drain the parked write"

    assert "session.usage_write_failed" in reported, (
        f"a failed usage write must be reported, got {reported}")


async def test_usage_record_lands_in_the_session_file(workspace, isolated_home):
    """The happy path: the record is on disk once the turn ends."""
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn
    from oaset.tui.app import OasetApp

    provider = MockProvider([MockTurn(
        content_chunks=["hi"],
        usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})])
    app = OasetApp(cfg=default_config(), cwd=workspace, provider=provider,
                   model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.4)
        app.submit_text("hello")
        for _ in range(40):
            await pilot.pause(0.1)
            if not app._worker_running():
                break
        await pilot.pause(0.3)

    files = [p for p in isolated_home.rglob("session_*.jsonl")
             if "index" not in p.name]
    assert files, "no session transcript was written"
    body = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in files)
    assert '"type": "usage"' in body, body[-400:]
    assert '"total_tokens": 7' in body


@pytest.mark.parametrize("name", ["_models_menu"])
def test_no_duplicate_screens(name):
    """A second implementation of the same screen is how "I fixed it but
    nothing changed" happens."""
    from oaset.tui.controllers import model_admin

    assert not hasattr(model_admin.ModelAdminMixin, name)
