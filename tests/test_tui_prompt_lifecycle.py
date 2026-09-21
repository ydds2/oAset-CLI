"""Prompt lifecycle, switch honesty and status-bar layout (B1/B3/B4 fixes).

The input wedge users hit twice had one structural cause: prompts had no single
owner. Two could be mounted at once, and one whose future was already consumed
was never removed - it kept focus and swallowed every keystroke while the event
loop looked perfectly alive. These tests pin the invariants that make that
impossible.

B3: a model switch persists the default, and says so; a failed write is reported
    instead of silently reverting on the next launch.
B4: the status bar measures rich markup by its visible width, so a long error
    cannot overlap the model/cwd on the left.
"""

from __future__ import annotations

from oaset.config import default_config
from oaset.providers.mock import MockProvider, MockTurn
from oaset.tui.app import OasetApp
from oaset.tui.widgets.chat import NoticeCard
from oaset.tui.widgets.inline import InlineInput, InlinePicker, InlinePromptBase
from oaset.tui.widgets.status_bar import clip, visible_len


def make_app(workspace):
    return OasetApp(cfg=default_config(), cwd=workspace,
                    provider=MockProvider([MockTurn(content_chunks=["ok"])]),
                    model_id="mock/mock-echo")


# ------------------------------------------------ B1: one prompt at a time

async def test_a_second_prompt_supersedes_the_first(workspace):
    """Two prompts must never coexist: the older one is closed, not left behind."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        first = InlinePicker("first", [("a", "A"), ("b", "B")])
        task = app.run_worker(app._run_prompt(first), exclusive=False)
        await pilot.pause(0.3)
        assert len(list(app.query(InlinePromptBase))) == 1

        second = InlineInput("second")
        app.run_worker(app._run_prompt(second), exclusive=False)
        await pilot.pause(0.3)

        prompts = list(app.query(InlinePromptBase))
        assert len(prompts) == 1, f"stacked prompts: {prompts}"
        assert isinstance(prompts[0], InlineInput), "the newest prompt owns the slot"
        assert first.future.done(), "the superseded prompt is unwound, not orphaned"

        for prompt in list(app.query(InlinePromptBase)):
            prompt._resolve(None)
        await pilot.pause(0.2)
        task.cancel()


async def test_escape_dismisses_a_stale_prompt(workspace):
    """A prompt whose future was consumed must still disappear on Escape.

    This is the reported wedge: the panel stayed mounted, kept focus and ate
    every keystroke - pressing Escape appeared to do nothing.
    """
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        picker = InlinePicker("stale", [("a", "A")])
        app.run_worker(app._run_prompt(picker), exclusive=False)
        await pilot.pause(0.3)
        picker._resolve("a")          # the awaiting side is gone / already done
        await pilot.pause(0.2)
        assert picker.future.done()

        await app.action_interrupt()   # Escape
        await pilot.pause(0.3)
        assert not list(app.query(InlinePromptBase)), \
            "a stale prompt must be removed by Escape, not left eating input"
        assert picker.display is False


async def test_commands_do_not_stack_prompts(workspace):
    """Command workers are serialised, so their prompts cannot pile up."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        app.submit_text("/models add")     # opens the wizard (a picker)
        await pilot.pause(0.4)
        app.submit_text("/sessions")       # a second prompt-opening command
        await pilot.pause(0.5)
        assert len(list(app.query(InlinePromptBase))) <= 1, \
            "two commands must not leave two prompts mounted"
        for prompt in list(app.query(InlinePromptBase)):
            prompt._resolve(None)
            await pilot.pause(0.1)


async def test_interrupt_without_a_prompt_is_harmless(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await app.action_interrupt()
        await pilot.pause(0.1)
        assert not list(app.query(InlinePromptBase))


# ------------------------------------------- B3: the switch says what it saved

async def test_model_switch_reports_that_the_default_was_saved(workspace):
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await app._switch_model_verified("mock/mock-echo")
        await pilot.pause(0.2)
        text = " ".join(n.raw_text for n in app.chat.query(NoticeCard))
        assert "保存为默认" in text or "saved as the default" in text, text


async def test_failed_config_write_is_reported_not_swallowed(workspace, monkeypatch):
    """The switch still applies, but the user learns it will not survive a restart."""
    app = make_app(workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)

        def boom(_cfg):
            raise OSError("disk is read-only")

        monkeypatch.setattr("oaset.tui.controllers.model_admin.save_config", boom)
        await app._switch_model_verified("mock/mock-echo")
        await pilot.pause(0.2)
        notices = [n for n in app.chat.query(NoticeCard)]
        text = " ".join(n.raw_text for n in notices)
        assert "写入失败" in text or "could NOT be saved" in text, text
        assert any(n.level == "error" for n in notices), \
            "a failed persist must be an error-level notice, not silence"


# ------------------------------------------- B4: the status bar cannot overlap

def test_visible_len_ignores_rich_markup():
    assert visible_len("[red]✗ boom[/red]") == len("✗ boom")
    assert visible_len("plain") == 5


def test_clip_is_cell_aware():
    assert clip("x" * 200, 40) == "x" * 40
    assert clip("中文", 3) == "中"      # a double-width char needs two cells


async def test_long_error_is_capped_when_rendered(workspace):
    """The rendered error keeps to its half of the bar, and the left survives."""
    from oaset.tui.notice import UiNotice

    app = make_app(workspace)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause(0.1)
        app.status_bar.set_model("mock/mock-echo", "mock")
        app.status_bar.set_cwd(str(workspace))
        app.status_bar.set_error(UiNotice("E" * 300, "error"))
        await pilot.pause(0.2)
        rendered = str(app.status_bar.right1.content)
        assert visible_len(rendered) <= 40 + 6, (
            f"error not capped to half the bar: {visible_len(rendered)} cells")
        assert (str(workspace)[:8] in app.status_bar.left_text
                or "mock" in app.status_bar.left_text), \
            "the left side must keep its identity instead of being overwritten"
