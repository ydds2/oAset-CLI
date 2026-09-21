"""Component-hardening regressions from the 2026-09-16 third audit round.

Pins the fixes a fresh-eyes reviewer found in the never-before-audited
widget layer: paste-write crash protection, same-second paste collisions,
markup-injecting file names, the phantom Ctrl+X, pager Enter semantics on
a single page, and the size-optional paste marker.
"""

from __future__ import annotations

from oaset.tui.paste import PASTE_REF_RE, expand_paste_refs


def test_paste_marker_expands_without_a_size_count():
    assert PASTE_REF_RE.search("[pasted: C:/tmp/x.txt (12 chars)]")
    m = PASTE_REF_RE.search("[pasted: C:/tmp/x.txt]")
    assert m and m.group("path") == "C:/tmp/x.txt"


def test_paste_refs_expand_in_both_marker_shapes(tmp_path):
    f1 = tmp_path / "a.txt"
    f1.write_text("content-a", encoding="utf-8")
    draft = f"[pasted: {f1} (9 chars)] and [pasted: {f1}]"
    expanded, _refs = expand_paste_refs(
        draft, lambda p: (tmp_path / p).read_text(encoding="utf-8"))
    assert expanded.count("content-a") == 2


def test_pager_enter_quits_on_a_single_page():
    from oaset.tui.viewer import run_pager

    seen: list[str] = []
    keys = iter(["next", ""])  # Enter on the only page, then a guard key
    run_pager(["only page"], read_key=lambda: next(keys),
              write=seen.append)
    assert len(seen) == 1, "Enter on a single page must return, not no-op"


def test_pager_footer_on_a_single_page_does_not_advertise_paging():
    from oaset.i18n import set_language
    from oaset.tui.viewer import pager_footer

    set_language("en")
    footer = pager_footer(0, 1)
    assert "next" not in footer and "prev" not in footer
    assert "return" in footer


def test_paste_targets_from_the_same_second_do_not_collide(workspace, monkeypatch):
    """Two big pastes inside one second used to overwrite each other while
    BOTH markers in the draft expanded to the survivor's content."""
    import asyncio
    import re

    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp

    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")

    async def main():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.3)
            app.input_area.insert_collapsed_paste("first-" + "x" * 3000)
            app.input_area.insert_collapsed_paste("second-" + "y" * 3000)
            draft = app.input_area.text
            names = re.findall(r"\[pasted: ([^\]]+)\]", draft)
            assert len(names) == 2 and names[0] != names[1], \
                "same-second pastes must land in distinct files"

    asyncio.run(main())


async def test_unwritable_paste_home_degrades_to_inline_insert(workspace, monkeypatch):
    """A full disk / read-only home must not take the app down (the paste
    write path was the only unguarded one — reads were always protected)."""
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp

    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)

        def explode(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr("pathlib.Path.write_text", explode)
        app.input_area.insert_collapsed_paste("y" * 5000)
        await pilot.pause(0.2)
        assert app.is_running
        assert "y" in app.input_area.text, "content degrades into the draft"


async def test_bracket_file_name_cannot_break_the_path_picker(workspace, monkeypatch):
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp
    from oaset.tui.widgets.inline import InlinePicker

    (workspace / "报告[终稿].md").write_text("x", encoding="utf-8")
    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        picker = InlinePicker("files", [("x", "报告[终稿].md")])
        await app.mount(picker)
        picker._refresh()
        await pilot.pause(0.1)
        rendered = str(picker._body.render())  # raises MarkupError if unescaped
        assert "终稿" in rendered


async def test_ctrl_x_cuts_the_draft_into_the_clipboard(workspace, monkeypatch):
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui import clipboard as clipboard_mod
    from oaset.tui.app import OasetApp

    captured: list[str] = []

    def fake_set_text(text):
        captured.append(text)
        return None

    monkeypatch.setattr(clipboard_mod, "set_text", fake_set_text)
    cfg = default_config()
    app = OasetApp(cfg=cfg, cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app.input_area.load_text("cut me")
        app.input_area.cursor_location = (0, len("cut me"))
        await pilot.press("ctrl+x")
        await pilot.pause(0.3)
        assert captured == ["cut me"]
        assert app.input_area.text == ""


def test_no_hardcoded_mcp_status_strings_outside_the_catalogue():
    from pathlib import Path

    root = Path("src/oaset")
    hits: list[str] = []
    for path in root.rglob("*.py"):
        if path.name == "i18n.py":
            continue
        if '"MCP: not started"' in path.read_text(encoding="utf-8"):
            hits.append(str(path))
    assert not hits, f"hardcoded MCP status strings remain: {hits}"
