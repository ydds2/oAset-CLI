"""Regressions for the two data-loss findings (TUI review round 3).

1. A collapsed paste (`[pasted: <path> (<n> chars)]`) must be expanded back to
   the pasted text before the turn is created — the model cannot read
   `~/.oaset/pastes/`, so the marker must never be what reaches it.
2. `/rewind` must confirm before dropping turns, and an unusable argument must
   not silently mean "drop 1".
"""

from __future__ import annotations

from oaset.tui.paste import (
    expand_paste_refs,
    find_paste_refs,
    read_paste_file,
    resolve_draft,
)

# ------------------------------------------------------------------ placeholder


def test_find_paste_refs_reads_path_and_size():
    text = "看日志 [pasted: C:\\tmp\\pastes\\a.txt (120 chars)] 然后修"
    refs = find_paste_refs(text)
    assert len(refs) == 1
    assert refs[0].path == "C:\\tmp\\pastes\\a.txt"
    assert refs[0].chars == 120
    assert text[refs[0].start:refs[0].end] == "[pasted: C:\\tmp\\pastes\\a.txt (120 chars)]"


def test_find_paste_refs_ignores_ordinary_text():
    assert find_paste_refs("no marker here") == []
    assert find_paste_refs("") == []


def test_expand_replaces_marker_with_file_body():
    body = "line one\nline two\n"
    text, expanded = expand_paste_refs(
        "before [pasted: /p/a.txt (9 chars)] after",
        lambda p: body if p == "/p/a.txt" else None)
    assert text == f"before {body} after"
    assert len(expanded) == 1


def test_expand_keeps_marker_when_file_is_gone():
    text, expanded = expand_paste_refs(
        "[pasted: /p/gone.txt (9 chars)]", lambda p: None)
    assert text == "[pasted: /p/gone.txt (9 chars)]"
    assert expanded == []


def test_expand_handles_several_markers_and_surrounding_text():
    first = "[pasted: /p/1.txt (1 chars)]"
    second = "[pasted: /p/2.txt (1 chars)]"
    text, expanded = expand_paste_refs(
        f"A {first} B {second} C",
        lambda p: {"\\p\\1.txt": "X", "/p/1.txt": "X", "/p/2.txt": "Y"}.get(p))
    assert text == "A X B Y C"
    assert len(expanded) == 2


def test_read_paste_file_refuses_paths_outside_the_pastes_dir(tmp_path):
    """A user-editable marker must not become an arbitrary-file-read primitive."""
    pastes = tmp_path / "pastes"
    pastes.mkdir()
    inside = pastes / "ok.txt"
    inside.write_text("pasted body", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("do not leak", encoding="utf-8")

    assert read_paste_file(str(inside), pastes) == "pasted body"
    assert read_paste_file(str(secret), pastes) is None
    assert read_paste_file(str(tmp_path / "missing.txt"), pastes) is None


def test_resolve_draft_end_to_end(tmp_path):
    pastes = tmp_path / "pastes"
    pastes.mkdir()
    target = pastes / "paste-1.txt"
    target.write_text("LOG LINE 0\nLOG LINE 1\n", encoding="utf-8")
    draft = f"修这个 [pasted: {target} (22 chars)]"
    resolved, expanded = resolve_draft(draft, pastes)
    assert "LOG LINE 0" in resolved
    assert "[pasted:" not in resolved
    assert len(expanded) == 1


# --------------------------------------------------------------- submit path


async def test_pasted_content_reaches_the_provider(workspace, isolated_home):
    """The whole point: what the model receives is the paste, not the marker."""
    from oaset.config import default_config
    from oaset.providers import MockProvider, MockTurn
    from oaset.tui.app import OasetApp

    big = "".join(f"LOG LINE {i}: boom\n" for i in range(400))
    assert len(big) > 2000
    provider = MockProvider([MockTurn(content_chunks=["ok"])])
    app = OasetApp(cfg=default_config(), cwd=workspace, provider=provider,
                   model_id="mock/mock-echo")
    async with app.run_test(size=(110, 36)) as pilot:
        app.input_area.insert_collapsed_paste(big)
        await pilot.pause(0.2)
        draft = app.input_area.text
        assert "[pasted:" in draft, "precondition: the composer collapsed the paste"
        app.submit_text(draft)
        for _ in range(60):
            await pilot.pause(0.1)
            if not app._worker_running():
                break

    assert provider.requests, "the turn never reached the provider"
    sent = "\n".join(
        str(m.get("content"))
        for request in provider.requests
        for m in request
        if isinstance(m.get("content"), str))
    assert "LOG LINE 0: boom" in sent, "the pasted body must reach the model"
    assert "LOG LINE 399: boom" in sent, "the whole paste must arrive"
    assert "[pasted:" not in sent, "the marker must not reach the model"


async def test_missing_paste_file_warns_and_keeps_the_pointer(
        workspace, isolated_home):
    from tests.test_app_tui import make_app

    pastes = isolated_home / "pastes"
    pastes.mkdir()
    gone = pastes / "deleted.txt"
    from oaset.providers import MockTurn

    app = make_app(workspace, [MockTurn(content_chunks=["ok"])])
    async with app.run_test(size=(110, 36)) as pilot:
        app.submit_text(f"look [pasted: {gone} (10 chars)]")
        for _ in range(30):
            await pilot.pause(0.1)
            if not app._worker_running():
                break
        from oaset.tui.widgets.chat import NoticeCard

        notices = [w.raw_text for w in app.chat.query(NoticeCard)]
    joined = " ".join(notices)
    assert "无法读回" in joined or "could not be re-read" in joined


# ------------------------------------------------------------------- rewind


def test_rewind_is_confirmed_not_picker_exempt():
    """`rewind`'s argless path is a numeric prompt, not a "which one?" picker,
    so it must not be exempt from the confirmation page."""
    from oaset.tui.commands import _PICKER_DANGER, find_command

    assert "rewind" not in _PICKER_DANGER
    assert find_command("rewind").risk == "danger"
    assert find_command("rewind").needs_confirmation


async def test_rewind_with_bad_argument_drops_nothing(workspace):
    from oaset.providers import MockTurn
    from tests.test_app_tui import make_app

    app = make_app(workspace, [MockTurn(content_chunks=["answer one"])])
    async with app.run_test(size=(110, 36)) as pilot:
        app.submit_text("question one")
        for _ in range(40):
            await pilot.pause(0.1)
            if not app._worker_running():
                break
        before = len(app.conversation.messages)
        assert before == 2

        app.submit_text("/rewind abc")
        for _ in range(30):
            await pilot.pause(0.1)
        # `/rewind abc` is RISK_DANGER, so the confirm page opens first and
        # nothing is dropped while it waits; dismiss it and assert no loss.
        from oaset.tui.widgets.inline import InlinePromptBase

        for widget in list(app.query(InlinePromptBase)):
            widget._resolve(None)
            await pilot.pause(0.05)
        await pilot.pause(0.3)
        assert len(app.conversation.messages) == before, "no turn may be dropped"
