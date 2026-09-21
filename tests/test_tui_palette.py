"""TUI-01 (command palette with parameter forms) and TUI-02 (input/picker/IME)
regressions.

These lock down the palette/input defects recorded against docs/PLAN.zh-CN.md:
  * `action_palette` was a name/description picker that executed a no-argument
    command immediately — no usage, no ArgSpec form, no confirmation, no way
    back to the list;
  * the image picker applied `limit` BEFORE the suffix filter, so a workspace
    with many non-image files truncated valid images out of the list;
  * a rejected path reported a usage string and dead-ended the command;
  * Enter could post a string that a later IME commit had already superseded,
    and a cancelled form had no defined focus target.
"""

from __future__ import annotations

import json

import pytest

from oaset.config import default_config
from oaset.providers import MockProvider, MockTurn
from oaset.tui.app import OasetApp
from oaset.tui.widgets.inline import InlineInput, InlinePicker
from oaset.tui.widgets.palette import CUSTOM_CHOICE, InlineCommandConfirm, InlineCommandPalette


@pytest.fixture(autouse=True)
def isolated_config_path(tmp_path, monkeypatch):
    import oaset.config as cfgmod

    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "config.toml")


def make_app(workspace, language="en"):
    cfg = default_config()
    cfg.ui_language = language
    return OasetApp(cfg=cfg, cwd=workspace, provider=MockProvider(
        [MockTurn(content_chunks=["ok"], finish_reason="stop")]), model_id="mock/mock-echo")


def _highlight(palette, name: str):
    """Filter to `name` and put the cursor on that exact command row.

    The filter is a substring match over name/description/category/alias/
    subcommands, so several rows can legitimately match one query.
    """
    palette.filter_text = name
    palette.apply_filter()
    names = [c.name for c in palette.matches]
    assert name in names, f"{name!r} not offered: {names}"
    palette.index = names.index(name)
    palette._refresh()
    return palette


async def _open_palette(app, pilot, timeout=4.0):
    import time as _time

    app.cmd_palette("")
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline and not app.query(InlineCommandPalette):
        await pilot.pause(0.05)
    assert app.query(InlineCommandPalette), "palette did not open"
    return app.query_one(InlineCommandPalette)


# ----------------------------------------------------------- registry schema


def test_every_command_exposes_a_serialisable_schema():
    from oaset.tui.commands import COMMANDS

    for cmd in COMMANDS:
        schema = cmd.schema()
        json.dumps(schema, ensure_ascii=False)  # must round-trip
        assert schema["risk"] in ("safe", "write", "danger"), cmd.name
        assert schema["category"], cmd.name


def test_requires_args_commands_all_have_an_arg_spec():
    from oaset.tui.commands import COMMANDS, arg_spec_for

    missing = [c.name for c in COMMANDS if c.requires_args and arg_spec_for(c.name) is None]
    assert missing == [], f"requires_args without a form: {missing}"


def test_subcommand_commands_have_an_arg_spec_or_a_picker_handler():
    """Every command that documents subcommands must be reachable without
    memorising flags: a schema, or a handler that opens its own picker."""
    from oaset.tui.commands import COMMANDS, arg_spec_for

    self_picking = {"sessions", "tools-toggle", "model"}
    for cmd in COMMANDS:
        if not cmd.subcommands:
            continue
        assert arg_spec_for(cmd.name) is not None or cmd.name in self_picking, cmd.name


def test_find_command_resolves_names_and_aliases():
    from oaset.tui.commands import find_command

    assert find_command("mode").name == "mode"
    assert find_command("q").name == "exit"      # legacy global alias
    assert find_command("?").name == "help"      # per-command alias
    assert find_command("no-such-command") is None


def test_destructive_commands_are_flagged():
    from oaset.tui.commands import find_command

    for name in ("session-delete", "undo", "rewind"):
        cmd = find_command(name)
        assert cmd.risk == "danger", name
        assert cmd.needs_confirmation
    assert not find_command("status").needs_confirmation


# ---------------------------------------------------------------- palette UX


async def test_palette_shows_category_usage_aliases_source_and_risk(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        palette = await _open_palette(app, pilot)
        rendered = str(palette._body.content)
        assert "category:" in rendered and "source:" in rendered
        assert "risk:" in rendered and "aliases:" in rendered
        # the highlighted row's usage is visible for a command that has one
        palette.filter_text = "session-delete"
        palette.index = 0
        palette._refresh()
        rendered = str(palette._body.content)
        assert "session-delete" in rendered and "<id>" in rendered
        assert "destructive" in rendered


async def test_palette_filters_by_name_description_category_and_alias(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        palette = await _open_palette(app, pilot)
        for query, expected in (
            ("mode", "mode"),                 # name
            ("permission", "mode"),           # description
            ("computer", "desktop"),          # category
            ("quit", "exit"),                 # alias
        ):
            palette.filter_text = query
            palette.apply_filter()
            names = [c.name for c in palette.matches]
            assert expected in names, f"{query!r} -> {names}"


async def test_palette_typing_filters_and_backspace_recovers(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        palette = await _open_palette(app, pilot)
        for ch in "zzzz-no-match":
            await pilot.press(ch)
        await pilot.pause(0.1)
        assert palette.matches == []
        for _ in range(len("zzzz-no-match")):
            await pilot.press("backspace")
        await pilot.pause(0.1)
        assert palette.matches, "backspace must widen the result set again"
        assert palette.filter_text == ""


async def test_palette_enter_on_empty_result_does_not_leave(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        palette = await _open_palette(app, pilot)
        palette.filter_text = "zzzz-no-match"
        palette.index = 0
        palette._refresh()
        await pilot.press("enter")
        await pilot.pause(0.15)
        assert not palette.future.done(), "empty result must not resolve the palette"
        await pilot.press("escape")  # clear filter
        await pilot.pause(0.1)
        assert palette.filter_text == ""
        assert not palette.future.done()


async def test_palette_escape_clears_filter_then_exits(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        palette = await _open_palette(app, pilot)
        await pilot.press("s")
        await pilot.pause(0.05)
        assert palette.filter_text == "s"
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert palette.filter_text == "" and not palette.future.done()
        await pilot.press("escape")
        await pilot.pause(0.15)
        assert palette.future.done() and palette.future.result() is None
        assert not app.query(InlineCommandPalette), "cancelled palette must unmount"


async def test_palette_runs_a_safe_command_without_confirmation(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        palette = await _open_palette(app, pilot)
        _highlight(palette, "status")
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert not app.query(InlineCommandConfirm), "read-only commands run directly"
        assert not app.query(InlineCommandPalette), "the palette closes after running"
        rendered = " ".join(str(c.render()) for c in app.chat.children)
        assert "session" in rendered.lower() and "model" in rendered.lower()


async def test_palette_confirmation_declined_runs_nothing_and_returns(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        palette = await _open_palette(app, pilot)
        _highlight(palette, "clear")
        before = len(app.chat.children)
        await pilot.press("enter")
        await pilot.pause(0.3)
        confirm = app.query_one(InlineCommandConfirm)
        assert "/clear" in confirm.command_line
        assert confirm.effect, "the confirmation must state the side effect"
        await pilot.press("n")
        await pilot.pause(0.4)
        assert not app.query(InlineCommandConfirm)
        assert app.query(InlineCommandPalette), "declining returns to the palette"
        assert len(app.chat.children) == before, "/clear must not have run"


async def test_palette_confirmation_accepted_runs_the_command(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        palette = await _open_palette(app, pilot)
        _highlight(palette, "yolo")
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.query_one(InlineCommandConfirm)
        await pilot.press("y")
        await pilot.pause(0.4)
        assert app.mode == "auto", "accepted confirmation must execute the command"


async def test_palette_param_form_cancel_returns_without_executing(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        palette = await _open_palette(app, pilot)
        _highlight(palette, "title")
        await pilot.press("enter")
        await pilot.pause(0.3)
        form = app.query_one(InlineInput)  # requires_args -> structured form
        assert form.future is not None
        title_before = app.session.meta.title
        await pilot.press("escape")  # escape is the universal cancel
        await pilot.pause(0.4)
        assert not app.query(InlineInput)
        assert app.query(InlineCommandPalette), "cancelling a form goes back to the palette"
        assert title_before == app.session.meta.title


async def test_typing_q_in_a_text_field_is_text_not_cancel(workspace):
    """A text field must not eat a legitimate 'q' — Escape is its cancel key."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/title")
        import time as _time

        deadline = _time.monotonic() + 4.0
        while _time.monotonic() < deadline and not app.query(InlineInput):
            await pilot.pause(0.05)
        await pilot.press("q")
        await pilot.pause(0.15)
        assert app.query(InlineInput), "a plain q must not cancel the form"
        from conftest import wait_for_inline_input

        field = await wait_for_inline_input(pilot, app)
        assert field.value == "q"
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert not app.query(InlineInput)


async def test_palette_param_form_value_reaches_the_command(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        palette = await _open_palette(app, pilot)
        _highlight(palette, "title")
        await pilot.press("enter")
        await pilot.pause(0.3)
        for ch in "from the palette":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.4)
        # a write command shows the FINAL command line, argument included
        confirm = app.query_one(InlineCommandConfirm)
        assert confirm.command_line == "/title from the palette"
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert app.session.meta.title == "from the palette"


async def test_palette_executes_a_command_with_picked_choice_argument(workspace):
    """/update from the palette: enum form, then the read-only action runs."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        palette = await _open_palette(app, pilot)
        _highlight(palette, "update")
        await pilot.press("enter")
        await pilot.pause(0.3)
        chooser = app.query_one(InlinePicker)
        offered = [v for v, _ in chooser.options]
        assert {"check", "list", "plan", "refresh"} <= set(offered)
        assert CUSTOM_CHOICE in offered  # manual escape hatch
        await pilot.press("enter")       # first choice = check (read-only)
        await pilot.pause(0.6)


async def test_choice_form_manual_row_opens_a_text_field(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        palette = await _open_palette(app, pilot)
        _highlight(palette, "mode")
        await pilot.press("enter")
        await pilot.pause(0.3)
        chooser = app.query_one(InlinePicker)
        chooser.index = [v for v, _ in chooser.options].index(CUSTOM_CHOICE)
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.query(InlineInput), "the manual row must open a text field"
        await pilot.press("escape")
        await pilot.pause(0.3)


# ------------------------------------------------------- picker: filter order


def test_workspace_files_filters_by_suffix_before_applying_the_limit(workspace):
    from oaset.utils import workspace_files

    # the walk reaches root files before descending, so an early limit fills up
    # there and never reaches the one image
    for name in ("a.py", "b.py", "c.py", "d.py", "e.py"):
        (workspace / name).write_text("x", encoding="utf-8")
    bulk = workspace / "src"
    bulk.mkdir()
    for i in range(300):
        (bulk / f"mod_{i:03d}.py").write_text("x", encoding="utf-8")
    (bulk / "deep.png").write_bytes(b"\x89PNG")

    filtered = workspace_files(workspace, "", limit=5, suffixes=(".png",))
    assert filtered == ["src/deep.png"]

    # the old order (limit, then filter) loses it entirely
    legacy = workspace_files(workspace, "", limit=5)
    assert not [f for f in legacy if f.endswith(".png")]


async def test_image_picker_offers_an_image_behind_many_other_files(workspace):
    for name in ("a.py", "b.py", "c.py", "d.py", "e.py", "f.py"):
        (workspace / name).write_text("x", encoding="utf-8")
    bulk = workspace / "src"
    bulk.mkdir()
    for i in range(300):
        (bulk / f"mod_{i:03d}.py").write_text("x", encoding="utf-8")
    (bulk / "photo.png").write_bytes(b"\x89PNG fake")

    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/image")
        import time as _time

        deadline = _time.monotonic() + 4.0
        while _time.monotonic() < deadline and not app.query(InlinePicker):
            await pilot.pause(0.05)
        offered = [v for v, _ in app.query_one(InlinePicker).options]
        assert any(v.endswith("photo.png") for v in offered), offered[:5]
        assert any(v == "\x00custom-path" for v in offered), "manual path row must exist"
        await pilot.press("q")
        await pilot.pause(0.2)


async def test_image_picker_labels_show_size(workspace):
    (workspace / "photo.png").write_bytes(b"\x89PNG" + b"0" * 2048)
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/image")
        import time as _time

        deadline = _time.monotonic() + 4.0
        while _time.monotonic() < deadline and not app.query(InlinePicker):
            await pilot.pause(0.05)
        labels = [label for _, label in app.query_one(InlinePicker).options]
        assert any("photo.png" in lbl and "KB" in lbl for lbl in labels), labels
        await pilot.press("q")
        await pilot.pause(0.2)


def test_path_rejection_explains_each_failure(workspace):
    from oaset.i18n import set_language
    from oaset.tui.app import OasetApp
    from oaset.utils import MAX_IMAGE_BYTES

    set_language("en")  # this test asserts the English rendering explicitly

    suffixes = (".png", ".jpg")
    missing = OasetApp._path_rejection(workspace / "nope.png", suffixes)
    assert missing and "not an existing file" in missing

    (workspace / "notes.txt").write_text("x", encoding="utf-8")
    wrong = OasetApp._path_rejection(workspace / "notes.txt", suffixes)
    assert wrong and "not a supported image" in wrong

    big = workspace / "big.png"
    big.write_bytes(b"0" * (MAX_IMAGE_BYTES + 1))
    oversized = OasetApp._path_rejection(big, suffixes)
    assert oversized and "over the" in oversized

    ok = workspace / "ok.png"
    ok.write_bytes(b"\x89PNG")
    assert OasetApp._path_rejection(ok, suffixes) is None


async def test_cmd_image_reports_a_reason_instead_of_a_usage_dead_end(workspace):
    from oaset.tui.widgets.chat import NoticeCard

    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.cmd_image("does-not-exist.png")
        await pilot.pause(0.1)
        texts = " ".join(getattr(n, "raw_text", "") for n in app.chat.query(NoticeCard))
        assert "not an existing file" in texts, texts


# ------------------------------------------------------------ enter / IME


async def test_one_enter_sends_exactly_once(workspace):
    app = make_app(workspace)
    sent: list[str] = []
    app.submit_text = sent.append  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.input_area.load_text("hello")
        app.input_area.action_submit()
        app.input_area.action_submit()  # a repeated Enter must be a no-op
        await pilot.pause(0.3)
        assert sent == ["hello"], sent


async def test_ime_commit_supersedes_a_pending_send(workspace):
    """Enter that belongs to a composition must not post the partial string."""
    app = make_app(workspace)
    sent: list[str] = []
    app.submit_text = sent.append  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.input_area.load_text("nihao")          # pre-commit buffer
        app.input_area.action_submit()             # Enter seen during composition
        app.input_area.load_text("你好")            # the commit lands
        await pilot.pause(0.3)
        assert sent == [], "the superseded pre-commit string must not be sent"
        assert app.input_area.text == "你好", "the committed text must survive"
        app.input_area.action_submit()
        await pilot.pause(0.3)
        assert sent == ["你好"], "the next Enter sends the committed text once"


async def test_ctrl_j_newline_cancels_a_pending_send(workspace):
    app = make_app(workspace)
    sent: list[str] = []
    app.submit_text = sent.append  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.input_area.load_text("line one")
        app.input_area.action_submit()
        app.input_area.action_newline()
        await pilot.pause(0.3)
        assert sent == []
        assert "\n" in app.input_area.text


async def test_enter_on_empty_buffer_is_a_no_op(workspace):
    app = make_app(workspace)
    sent: list[str] = []
    app.submit_text = sent.append  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.input_area.load_text("   \n  ")
        app.input_area.action_submit()
        await pilot.pause(0.3)
        assert sent == []


# -------------------------------------------------------------- focus stack


async def test_focus_falls_back_to_input_when_the_previous_widget_is_gone(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        ghost = InlineInput("gone", "")
        app._restore_focus(ghost)  # never mounted: no focusable target
        assert app.focused is app.input_area


async def test_prompt_restores_the_focus_active_before_it(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.input_area.focus()
        assert app.focused is app.input_area
        worker = app.run_worker(app._pick("pick one", [("a", "A"), ("b", "B")]))
        await pilot.pause(0.2)
        assert app.focused is not app.input_area  # the form owns the keyboard
        await pilot.press("q")
        await pilot.pause(0.3)
        worker.cancel()
        assert app.focused is app.input_area, "focus must return to the pre-prompt widget"


# ------------------------------------------------------------ permission copy


async def test_permission_panel_states_every_key(workspace):
    from oaset.tui.widgets.inline import InlinePermission

    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        worker = app.run_worker(app.show_permission("write_file", "write", "write a.txt", "hello"))
        await pilot.pause(0.2)
        panel = app.query_one(InlinePermission)
        rendered = str(panel._body.content)
        assert "[b]y[/b]" in rendered and "[b]a[/b]" in rendered and "[b]n[/b]" in rendered
        assert "deny" in rendered.lower()
        await pilot.press("n")
        await pilot.pause(0.3)
        worker.cancel()
