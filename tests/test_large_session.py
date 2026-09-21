"""Large-session behaviour: a long running session must not stall the
event pump or grow the mounted DOM without bound.

The chat log caps live blocks (MAX_MOUNTED_BLOCKS, older ones detach into
the archive); this pins that the cap actually fires under a burst, that
the archive hint appears, and that the pump stays responsive throughout.
"""

from __future__ import annotations

import time

from oaset.config import default_config
from oaset.providers import MockProvider
from oaset.tui.app import OasetApp
from oaset.tui.widgets.chat import ChatLog, NoticeCard


async def test_long_session_caps_the_dom_and_stays_alive(workspace):
    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        chat: ChatLog = app.chat

        start = time.perf_counter()
        for i in range(800):
            chat.add_notice(f"burst line {i}", "info")
        await pilot.pause(1.5)   # let the cap's detach task drain
        burst_elapsed = time.perf_counter() - start

        assert app.is_running
        mounted = len(list(chat.query(NoticeCard)))
        assert mounted <= chat.max_mounted_blocks + 10, \
            f"{mounted} notice cards mounted; the cap did not fire"
        assert chat._archived, "older blocks were not archived"

        # the pump is still serving input after the burst (no dead UI)
        await pilot.pause(0.1)
        assert app.is_running
        assert burst_elapsed < 15.0, f"burst took {burst_elapsed:.1f}s — the UI stalled"


async def test_chat_follows_the_tail_through_the_burst(workspace):
    """In a long session the newest content must stay on screen — the log
    follows its tail unless the user scrolls up on purpose."""
    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        for i in range(800):
            app.chat.add_notice(f"burst line {i}", "info")
        await pilot.pause(1.0)
        strips = app.screen._compositor.render_strips()
        screen = "\n".join("".join(seg.text for seg in strip) for strip in strips)
        assert "burst line 799" in screen, "the tail scrolled out of view"


def test_notice_clamps_huge_bodies_but_copy_keeps_everything():
    """A tool dump or /history must not fill the screen: display clamps at
    MAX_SHOWN_LINES, the full body survives for selection copy."""
    from oaset.tui.widgets.chat import NoticeCard

    body = "\n".join(f"line {i} " + "x" * 200 for i in range(200))
    card = NoticeCard(body, "info")
    shown = str(card.render())
    assert shown.count("\n") <= NoticeCard.MAX_SHOWN_LINES + 2
    assert "line 199" not in shown
    assert "line 199" in card.copy_text()


def test_store_roundtrips_a_thousand_message_session(workspace):
    """Long sessions must persist and reload intact — and quickly enough
    that autosave never feels like a stall."""
    from oaset.agent.messages import Conversation, Message
    from oaset.session.store import SessionStore

    store = SessionStore()
    conv = Conversation()
    for i in range(1000):
        conv.append(Message(role="user" if i % 2 else "assistant", content=f"m{i}"))

    session = store.new_session(workspace, "mock/mock-echo")
    start = time.perf_counter()
    for message in conv.messages:
        store.append_message(session, message)
    elapsed = time.perf_counter() - start
    assert elapsed < 10.0, f"1000 appends took {elapsed:.1f}s"

    loaded = store.load(session.meta.session_id)
    assert loaded is not None
    assert [str(m.content) for m in loaded.conversation.messages] == \
        [f"m{i}" for i in range(1000)]
