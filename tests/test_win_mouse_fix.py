"""Windows mouse fix: console-mode mask and MOUSE_EVENT translation.

The translator must produce the same event sequence the XTerm parser yields on
Linux — press/release/move with Textual button numbering (1=left, 2=middle,
3=right) and the four scroll directions — so the TUI's mouse behaviour is
identical across platforms.
"""

from __future__ import annotations

import sys

import pytest

from oaset.tui._win_mouse_fix import (
    ENABLE_MOUSE_INPUT,
    ENABLE_VIRTUAL_TERMINAL_INPUT,
    ENABLE_WINDOW_INPUT,
    FIXED_INPUT_MODE,
    MouseEventTranslator,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows console input records"
)


def test_fixed_input_mode_keeps_mouse_and_window_bits() -> None:
    assert FIXED_INPUT_MODE & ENABLE_VIRTUAL_TERMINAL_INPUT
    assert FIXED_INPUT_MODE & ENABLE_MOUSE_INPUT
    assert FIXED_INPUT_MODE & ENABLE_WINDOW_INPUT
    # QuickEdit (0x40) must stay OFF: the console would eat mouse input for
    # its own text selection instead of reporting records to the app.
    assert not FIXED_INPUT_MODE & 0x0040


def test_left_click_press_and_release() -> None:
    t = MouseEventTranslator()
    down = t.feed(flags=0, button_state=0x0001, control_key_state=0, x=10, y=5)
    assert down == [("down", 10, 5, 1, False, False, False)]
    up = t.feed(flags=0, button_state=0x0000, control_key_state=0, x=10, y=5)
    assert up == [("up", 10, 5, 1, False, False, False)]


def test_button_numbering_follows_textual() -> None:
    t = MouseEventTranslator()
    # console: left=0x1, middle(2nd)=0x4, right=0x2 — textual: 1, 2, 3
    assert t.feed(0, 0x0001, 0, 0, 0)[0][3] == 1
    assert t.feed(0, 0x0005, 0, 0, 0)[0][3] == 2  # middle added, left still held
    assert t.feed(0, 0x0007, 0, 0, 0)[0][3] == 3  # right added
    ups = t.feed(0, 0x0000, 0, 0, 0)
    assert [e[3] for e in ups] == [1, 2, 3]


def test_hover_move_carries_no_button_and_drag_carries_it() -> None:
    t = MouseEventTranslator()
    t.feed(0, 0x0000, 0, 3, 3)
    move = t.feed(flags=0x0001, button_state=0x0000, control_key_state=0, x=4, y=3)
    assert move == [("move", 4, 3, 0, False, False, False)]
    press = t.feed(0, 0x0001, 0, 4, 3)  # press
    assert press == [("down", 4, 3, 1, False, False, False)]
    drag = t.feed(flags=0x0001, button_state=0x0001, control_key_state=0, x=6, y=4)
    assert drag == [("move", 6, 4, 1, False, False, False)]


def test_wheel_direction_from_signed_high_word() -> None:
    t = MouseEventTranslator()
    up = t.feed(flags=0x0004, button_state=0x00780000, control_key_state=0, x=0, y=0)
    assert up == [("wheel_up", 0, 0, 0, False, False, False)]
    down = t.feed(flags=0x0004, button_state=0xFF780000, control_key_state=0, x=0, y=0)
    assert down == [("wheel_down", 0, 0, 0, False, False, False)]


def test_modifier_bits_translate_to_shift_meta_ctrl() -> None:
    t = MouseEventTranslator()
    event = t.feed(
        flags=0, button_state=0x0002, control_key_state=0x001A, x=1, y=1
    )  # shift 0x10 | left-alt 0x2 | left-ctrl 0x8
    assert event == [("down", 1, 1, 3, True, True, True)]


def test_double_click_flag_is_just_a_press() -> None:
    t = MouseEventTranslator()
    first = t.feed(flags=0x0000, button_state=0x0001, control_key_state=0, x=2, y=2)
    second = t.feed(flags=0x0002, button_state=0x0000, control_key_state=0, x=2, y=2)
    third = t.feed(flags=0x0002, button_state=0x0001, control_key_state=0, x=2, y=2)
    assert first[0][0] == "down"
    assert second[0][0] == "up"
    assert third[0][0] == "down"  # no synthetic duplicate; app times the chain


def test_wheel_leaves_button_state_untouched() -> None:
    t = MouseEventTranslator()
    t.feed(0, 0x0001, 0, 0, 0)  # left button held
    scrolled = t.feed(0x0004, 0x00780000, 0, 1, 1)
    assert scrolled[0][0] == "wheel_up"
    # release still reported correctly afterwards (state was not clobbered)
    up = t.feed(0, 0x0000, 0, 1, 1)
    assert up == [("up", 1, 1, 1, False, False, False)]


async def test_stale_picker_click_hides_instead_of_swallowing(workspace):
    """A picker whose worker died must not linger swallowing clicks.

    Reported on the real machine: a /model picker left in the stale state made
    "查看已配置的 provider 与模型" un-expandable — every click only moved the
    highlight. Now the first click on a stale picker hides it.
    """
    import pytest

    pytest.importorskip("textual")
    from oaset.tui.widgets.inline import InlinePicker

    app = make_app(workspace)
    async with app.run_test(size=(110, 36)) as pilot:
        picker = InlinePicker("pick", [("a", "A"), ("__list__", "LIST")])
        await app.mount(picker)
        await pilot.pause()
        assert picker._line_map, "render must build the line map"

        picker._resolve("a")  # the awaiting worker consumed the first choice
        class _E:
            y = next(ln for ln, tgt in picker._line_map.items()
                     if picker.options[tgt][0] == "__list__")
            def stop(self): pass
        picker.on_click(_E())
        await pilot.pause()
        assert picker.display is False, "stale picker must hide on click"


def make_app(workspace):
    from oaset.config import default_config
    from oaset.tui.app import OasetApp
    cfg = default_config()
    return OasetApp(cfg=cfg, cwd=workspace)


def test_report_event_monitor_death_reaches_the_app():
    """Freeze-fix regression: when the input monitor gives up after repeated
    crashes, the user must SEE an error — a dead console looks like a frozen
    TUI. Only-log was the bug."""
    import threading

    from oaset.tui._win_mouse_fix import report_event_monitor_death

    class FakeApp:
        def __init__(self):
            self.calls: list = []
            self.loop_thread = threading.current_thread()

        def call_from_thread(self, fn, *args, **kwargs):
            self.calls.append(("marshalled", fn, args, kwargs))
            fn(*args, **kwargs)

        def notify_error(self, exc, source="", code="", hint=""):
            self.calls.append(("notify", str(exc), source, code, hint))

    app = FakeApp()
    report_event_monitor_death(app)
    kinds = [c[0] for c in app.calls]
    assert "marshalled" in kinds          # marshalled off the monitor thread
    notify = next(c for c in app.calls if c[0] == "notify")
    assert notify[2] == "input" and notify[3] == "event_monitor.dead"
    assert "重启" in notify[1] or "Restart" in notify[1] or notify[1]
