"""The block cap must actually bound the DOM, and archive pages must round-trip.

Round 3 of the TUI review found `_enforce_cap()` moved blocks into `_archived`
but the async detach task never ran: 900 mounted blocks left 300 widgets
flagged as archived *and still mounted*, so per-frame layout cost kept growing
with the session — the exact failure MAX_MOUNTED_BLOCKS exists to prevent.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


async def test_mounted_dom_is_bounded_by_the_cap(workspace):
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.4)
        chat = app.chat
        total = chat.max_mounted_blocks + 300
        for i in range(total):
            run = app.activity_start(f"t{i}", "read_file", f"src/file{i}.py")
            app.activity_finish(f"t{i}", f"contents {i}", False, run)
        for _ in range(40):  # let the deferred mounts settle
            await pilot.pause(0.05)

        mounted = len(chat._live_blocks())
        assert mounted <= chat.max_mounted_blocks + 2, (
            f"{total} blocks mounted left {mounted} in the DOM "
            f"(cap {chat.max_mounted_blocks})")
        assert chat._archived, "older blocks must be archived, not dropped"

        # Nothing may be both archived and still mounted.
        stale = [w for w in chat.children if getattr(w, "_oaset_archived", False)]
        assert not stale, f"{len(stale)} blocks are archived but still in the DOM"


async def test_archive_holds_each_block_exactly_once(workspace):
    """A burst used to append the same widgets to _archived on every mount."""
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.4)
        chat = app.chat
        for i in range(chat.max_mounted_blocks + 200):
            run = app.activity_start(f"t{i}", "read_file", f"src/f{i}.py")
            app.activity_finish(f"t{i}", f"c{i}", False, run)
        for _ in range(40):
            await pilot.pause(0.05)

        archived = chat._archived
        unique = {id(w) for w in archived}
        assert len(archived) == len(unique), (
            f"_archived holds {len(archived)} refs for {len(unique)} objects")


async def test_archived_page_remounts_at_the_top(workspace):
    """Scrolling back to the top must bring the oldest blocks back, in order.

    Uses text blocks rather than activity cards: an activity card collapses to a
    single line, so 600 of them fit the viewport and there is nothing to scroll —
    a transcript is what makes the archive page reachable.
    """
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.4)
        await _fill_transcript(app, pilot, 900)

        chat = app.chat
        assert chat._archived, "precondition: something was archived"
        assert chat.max_scroll_y > 0, "precondition: the transcript scrolls"

        archived_before = len(chat._archived)
        chat.scroll_home(animate=False)
        await pilot.pause(0.2)
        for _ in range(40):
            await pilot.pause(0.05)
            if len(chat._archived) < archived_before:
                break

        assert len(chat._archived) < archived_before, \
            "reaching the top must remount a page"
        # The remounted blocks rejoin the live census; otherwise the next mount
        # would archive them a second time.
        stale = [w for w in chat.children if getattr(w, "_oaset_archived", False)]
        assert not stale, f"{len(stale)} remounted blocks still look archived"


async def _fill_transcript(app, pilot, count: int) -> None:
    for i in range(count):
        app.chat.add_notice(f"notice line {i}", "info")
        if i % 50 == 0:
            await pilot.pause(0.05)
    for _ in range(30):
        await pilot.pause(0.05)
