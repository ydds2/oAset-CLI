"""Discoverability: recently used commands and "which code am I running?".

Two user questions this pins down:

- "what did I use last time?" - the palette leads with the commands this user
  actually runs, instead of making them re-read 53 entries;
- "is this the build with my fix?" - /status and /version name the running
  source, because an editable checkout and a frozen exe can differ by weeks.
"""

from __future__ import annotations

from oaset.config import default_config
from oaset.providers.mock import MockProvider, MockTurn
from oaset.tui import usage
from oaset.tui.app import OasetApp
from oaset.tui.widgets.chat import NoticeCard


def make_app(workspace):
    return OasetApp(cfg=default_config(), cwd=workspace,
                    provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                    model_id="mock/mock-echo")


# ------------------------------------------------------------------- usage

def test_recent_is_empty_before_anything_runs():
    assert usage.recent() == []


def test_record_orders_by_recency_and_counts_hits():
    usage.record("model")
    usage.record("status")
    usage.record("model")  # most recent wins
    assert usage.recent(limit=5) == ["model", "status"]


def test_recent_respects_the_limit():
    for name in ("help", "status", "usage", "version", "tools", "theme"):
        usage.record(name)
    assert len(usage.recent(limit=3)) == 3


def test_corrupt_usage_file_is_tolerated():
    """Convenience state must never break a command."""
    path = usage._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all", encoding="utf-8")
    assert usage.recent() == []
    usage.record("help")  # must not raise
    assert usage.recent() == ["help"]


# ----------------------------------------------------------------- palette

async def test_palette_leads_with_recently_used_commands(workspace):
    from oaset.tui.commands import COMMANDS
    from oaset.tui.widgets.palette import InlineCommandPalette

    usage.record("theme")
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        picker = InlineCommandPalette(COMMANDS)
        await app.mount(picker)
        await pilot.pause(0.2)
        assert picker.matches, "palette must list commands"
        assert picker.matches[0].name == "theme", \
            "the most recent command should be first in the unfiltered view"
        body = str(picker._body.content)
        assert "最近使用" in body or "Recently used" in body
        # filtering still spans everything (recent group disappears)
        picker.filter_text = "sett"
        picker.apply_filter()
        assert picker.matches and picker.matches[0].name == "settings"


# ------------------------------------------------------- running source

def test_running_source_names_the_checkout():
    from oaset.runtime_info import describe_source, running_source

    info = running_source()
    # semantics: a checkout is 'dev', a stamped packaged build is 'release'
    assert info["kind"] in ("dev", "release", "unknown"), info
    text = describe_source()
    assert "oaset" in text.lower() or info["path"]


async def test_status_and_version_report_the_running_source(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/status")
        await pilot.pause(0.4)
        app.submit_text("/version")
        await pilot.pause(0.4)

        text = "\n".join(str(c.content) for c in app.chat.query("Static.chat-card"))
        text += "\n" + "\n".join(n.raw_text for n in app.chat.query(NoticeCard))
        assert ("source" in text) or ("frozen" in text), \
            f"neither /status nor /version named the running source: {text[:400]}"
