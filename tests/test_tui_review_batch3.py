"""Regressions for the follow-up TUI fixes.

* `/memory` no longer truncates silently and offers the full text.
* `/search` hits are actionable instead of a dead-end list.
* `/status` columns line up in Chinese as well as English.
"""

from __future__ import annotations

import inspect

import pytest

# ----------------------------------------------------------------- /memory


def test_memory_preview_reports_what_it_hid():
    from oaset.i18n import CATALOG
    from oaset.tui.controllers import content_cmds

    src = inspect.getsource(content_cmds.ContentCmdsMixin.cmd_memory)
    assert "MEMORY_PREVIEW_CHARS" in src, "the preview length must be a named bound"
    assert "memory_truncated" in src, "the user must be told content was hidden"
    assert "memory_view_hint" in src, "there must be a path to the full text"
    for key in ("memory_truncated", "memory_view_hint", "memory_viewer_title"):
        assert CATALOG[key].get("en") and CATALOG[key].get("zh"), key


async def test_memory_over_preview_says_how_much_is_hidden(workspace, isolated_home):
    """A 2000-char silent cut is what this replaces."""
    from oaset.i18n import t
    from oaset.tui.widgets.chat import NoticeCard
    from tests.test_app_tui import make_app

    (isolated_home / "memory").mkdir(parents=True, exist_ok=True)
    (isolated_home / "memory" / "MEMORY.md").write_text(
        "".join(f"fact {i}: something remembered\n" for i in range(300)),
        encoding="utf-8")

    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        await app.cmd_memory("")
        await pilot.pause(0.3)
        notices = [w.raw_text for w in app.chat.query(NoticeCard)]
    joined = "\n".join(notices)
    assert "fact 0:" in joined, "the preview must show the start of the memory"
    assert t("memory_view_hint") in joined, "the user must be pointed at /memory all"
    # the withheld amount must be stated, in the active language
    assert t("memory_truncated", n=123).split("123")[0][:4] in joined, \
        "the preview must state that content was withheld"


async def test_memory_all_opens_the_viewer(workspace, isolated_home, monkeypatch):
    from oaset.tui import viewer
    from tests.test_app_tui import make_app

    (isolated_home / "memory").mkdir(parents=True, exist_ok=True)
    (isolated_home / "memory" / "MEMORY.md").write_text("x" * 5000, encoding="utf-8")

    seen: dict[str, str] = {}

    async def fake_view(app, title, text):
        seen["title"] = title
        seen["text"] = text

    monkeypatch.setattr(viewer, "view_text", fake_view)
    app = make_app(workspace, [])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        await app.cmd_memory("all")
        await pilot.pause(0.3)

    assert seen, "/memory all must open the viewer"
    assert "MEMORY.md" in seen["text"]
    assert len(seen["text"]) > 4000, "the viewer gets the FULL text, not the preview"


# ----------------------------------------------------------------- /search


def test_search_offers_to_open_a_hit():
    from oaset.tui.controllers import session_admin

    src = inspect.getsource(session_admin.SessionAdminMixin.cmd_search)
    assert "_pick(" in src, "hits must be actionable"
    assert "_load_session(" in src, "picking a hit must open that session"
    assert "search_open_title" in src


async def test_search_pick_opens_the_session(workspace, monkeypatch):
    """The hit list used to end in 'copy the id and run /sessions'."""
    from oaset.history import HistoryHit
    from oaset.tui.controllers import session_admin as sa_mod
    from tests.test_app_tui import make_app

    app = make_app(workspace, [])
    opened: list[str] = []

    async def fake_load(session_id: str) -> None:
        opened.append(session_id)

    async def fake_pick(title, options, **kw):
        assert options, "the picker must be offered the hits"
        return options[0][0]

    monkeypatch.setattr(sa_mod, "search_history",
                        lambda q, limit=8: [HistoryHit(session_id="sid-1234", path="",
                                                      title="found it",
                                                      role="user",
                                                      snippet="needle")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app._load_session = fake_load  # type: ignore[method-assign]
        app._pick = fake_pick  # type: ignore[method-assign]
        await app.cmd_search("needle")
        await pilot.pause(0.2)

    assert opened == ["sid-1234"], f"picking a hit must open it, got {opened}"


async def test_search_cancel_keeps_the_list(workspace, monkeypatch):
    from oaset.history import HistoryHit
    from oaset.tui.controllers import session_admin as sa_mod
    from oaset.tui.widgets.chat import NoticeCard
    from tests.test_app_tui import make_app

    app = make_app(workspace, [])

    async def fake_pick(title, options, **kw):
        return None  # the user pressed Esc

    monkeypatch.setattr(sa_mod, "search_history",
                        lambda q, limit=8: [HistoryHit(session_id="sid-9999", path="",
                                                      title="t", role="user",
                                                      snippet="s")])
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        app._pick = fake_pick  # type: ignore[method-assign]
        await app.cmd_search("needle")
        await pilot.pause(0.2)
        notices = "\n".join(w.raw_text for w in app.chat.query(NoticeCard))
    assert "sid-9999" in notices or "9999" in notices, \
        "cancelling must leave the hit list visible"


# ------------------------------------------------- /status label alignment


def _status_card_text(app) -> str:
    """The /status card body as plain text.

    Textual's Static keeps the value it was given in `.content`; `render()`
    returns a Blank for this widget, so reading that yields an empty string.
    """
    card = app.chat.children[-1]
    return str(getattr(card, "content", "") or "")


@pytest.mark.parametrize("lang", ["zh", "en"])
async def test_status_columns_line_up_in_both_languages(workspace, lang):
    """Labels were padded by character count, so the Chinese label ("工作目录"
    is 4 characters but 8 columns) pushed its value one column right and every
    row after the first was misaligned.

    The language is set AFTER the app mounts: on_mount() re-applies
    `cfg.ui_language`, so setting it beforehand is silently overwritten and the
    English arm would just re-test Chinese.
    """
    from rich.cells import cell_len

    from oaset.i18n import resolve_language, set_language
    from tests.test_app_tui import make_app

    app = make_app(workspace, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        set_language(lang)
        assert resolve_language() == lang, "precondition: the UI language is pinned"
        app.cmd_status("")
        await pilot.pause(0.3)
        body = _status_card_text(app)
    set_language("zh")

    rows = [ln for ln in body.splitlines() if ln.startswith("  ") and ln.strip()]
    assert len(rows) >= 4, f"/status produced too few rows: {body!r}"

    # Every value must begin in the same column. Padding by character count
    # instead of display width broke this for the CJK labels.
    offsets = set()
    labels = []
    for row in rows:
        head = row[2:]
        pad = len(head) - len(head.lstrip(" "))
        offsets.add(pad)
        labels.append(cell_len(head[:pad].rstrip()) if pad else 0)
    assert len(offsets) == 1, (
        "every /status row must start its value in the same column; got "
        f"offsets {sorted(offsets)} for:\n{body}")

    value_column = offsets.pop()
    widest = max(labels)
    assert value_column >= widest, (
        f"value column {value_column} is narrower than the widest label "
        f"({widest}) for:\n{body}")
