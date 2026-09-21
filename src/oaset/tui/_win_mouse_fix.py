"""Windows mouse support for the oAset TUI — controlled fix for an upstream gap.

textual 8.x cannot deliver mouse input on Windows at all. Verified against
textual 8.2.8 on Windows Terminal 1.24 and conhost (real-machine capture
reproducible via scripts/acceptance_capture.py, see docs/evidence/):

1. ``win32.enable_application_mode()`` sets the stdin console mode to exactly
   ``ENABLE_VIRTUAL_TERMINAL_INPUT``, wiping ``ENABLE_MOUSE_INPUT``. Under
   ConPTY the host only materialises MOUSE_EVENT records into the client's
   input queue when that bit is set, so clicks never reach the app (observed
   input mode: 512, real foreground clicks ignored in both hosts).
2. ``win32.EventMonitor.run()`` handles KEY_EVENT and
   WINDOW_BUFFER_SIZE_EVENT but silently drops MOUSE_EVENT records — so even
   with the mode fixed the records would be discarded.

This module fixes both, without touching site-packages: a corrected console
mode (VT input + mouse + window input, QuickEdit off) and a mouse-aware
EventMonitor that translates MOUSE_EVENT records into the same Textual events
the XTerm parser produces on Linux (MouseDown/MouseUp/MouseMove and the four
scroll events, identical constructor shape and button numbering). Everything
else in upstream behaviour is left untouched. Applying the patch is idempotent
and a no-op on non-Windows platforms.

The record→event translation lives in :class:`MouseEventTranslator`, a pure
stateful mapper over plain ints so it can be unit-tested without a console
(tests/test_win_mouse_fix.py).
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from asyncio import run_coroutine_threadsafe
from typing import TYPE_CHECKING, Any, Callable, List, Optional, cast

if TYPE_CHECKING:  # pragma: no cover
    from textual.app import App

# --- console input mode bits (mirror of win32.py constants) -----------------

ENABLE_WINDOW_INPUT = 0x0008
ENABLE_MOUSE_INPUT = 0x0010
# QuickEdit must stay OFF (it swallows mouse clicks). Per MSDN the QuickEdit
# bit is only honoured when ENABLE_EXTENDED_FLAGS is also set, so the extended
# flag has to be part of the fixed mode - without it the mode write above is
# a no-op on the QuickEdit bit.
ENABLE_QUICK_EDIT_MODE = 0x0040  # deliberately absent from FIXED_INPUT_MODE
ENABLE_EXTENDED_FLAGS = 0x0080
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

FIXED_INPUT_MODE = (
    ENABLE_VIRTUAL_TERMINAL_INPUT | ENABLE_MOUSE_INPUT | ENABLE_WINDOW_INPUT
    | ENABLE_EXTENDED_FLAGS
)

# --- MOUSE_EVENT_RECORD fields ----------------------------------------------

MOUSE_MOVED = 0x0001
DOUBLE_CLICK = 0x0002  # a press record that arrived as part of a double click
MOUSE_WHEELED = 0x0004
MOUSE_HWHEELED = 0x0008

# dwButtonState low word bits → Textual button index (1=left, 2=middle, 3=right,
# 4/5 = extra buttons; console numbers them left/right/middle/3rd/4th).
_BUTTON_BITS: tuple[tuple[int, int], ...] = (
    (0x0001, 1),  # FROM_LEFT_1ST_BUTTON_PRESSED → left
    (0x0004, 2),  # FROM_LEFT_2ND_BUTTON_PRESSED → middle
    (0x0002, 3),  # RIGHTMOST_BUTTON_PRESSED → right
    (0x0008, 4),  # FROM_LEFT_3RD_BUTTON_PRESSED
    (0x0010, 5),  # FROM_LEFT_4TH_BUTTON_PRESSED
)

_SHIFT_PRESSED = 0x0010
_ALT_PRESSED = 0x0003  # LEFT_ALT | RIGHT_ALT
_CTRL_PRESSED = 0x000C  # LEFT_CTRL | RIGHT_CTRL

# Translation output tuples (plain data so tests need no Textual objects):
#   ("down"|"up"|"move", x, y, button, shift, meta, ctrl)
#   ("wheel_up"|"wheel_down"|"wheel_left"|"wheel_right", x, y, 0, shift, meta, ctrl)
MouseTuple = tuple


def _button_numbers(button_state: int) -> tuple[int, ...]:
    """Textual button indexes currently held, in console bit order."""
    return tuple(b for bit, b in _BUTTON_BITS if button_state & bit)


def _signed_high_word(button_state: int) -> int:
    """The wheel delta lives in the high WORD of dwButtonState (±120)."""
    high = (button_state >> 16) & 0xFFFF
    return high - 0x10000 if high & 0x8000 else high


class MouseEventTranslator:
    """Stateful MOUSE_EVENT_RECORD → event-tuple translator.

    Keeps the previous button state so a record can be classified as press,
    release or drag/move — the console record itself does not say which.
    """

    def __init__(self) -> None:
        self._last_button_state = 0

    def feed(
        self,
        flags: int,
        button_state: int,
        control_key_state: int,
        x: int,
        y: int,
    ) -> List[MouseTuple]:
        mods = (
            bool(control_key_state & _SHIFT_PRESSED),
            bool(control_key_state & _ALT_PRESSED),
            bool(control_key_state & _CTRL_PRESSED),
        )
        out: List[MouseTuple] = []

        if flags & MOUSE_WHEELED:
            direction = "wheel_up" if _signed_high_word(button_state) > 0 else "wheel_down"
            out.append((direction, x, y, 0, *mods))
            return out
        if flags & MOUSE_HWHEELED:
            direction = (
                "wheel_right" if _signed_high_word(button_state) > 0 else "wheel_left"
            )
            out.append((direction, x, y, 0, *mods))
            return out

        held = _button_numbers(button_state)
        previous = _button_numbers(self._last_button_state)
        moved = bool(flags & MOUSE_MOVED)

        if moved and held == previous:
            # Pure motion (hover when nothing held, drag when a button is).
            button = held[0] if held else 0
            out.append(("move", x, y, button, *mods))
        elif held != previous:
            for button in held:
                if button not in previous:
                    out.append(("down", x, y, button, *mods))
            for button in previous:
                if button not in held:
                    out.append(("up", x, y, button, *mods))
            if held and moved:
                # Press moved with the button down: Textual wants the drag as
                # a MouseMove carrying the held button.
                out.append(("move", x, y, held[0], *mods))
        # A DOUBLE_CLICK flag marks a press record; the state diff above
        # already emits its MouseDown (App-level click chains are synthesised
        # from down/up timing, so no extra event is needed here).

        self._last_button_state = button_state
        return out


def report_event_monitor_death(app: Any) -> None:
    """Tell the USER (not just the log) that the input monitor gave up.

    1000 rapid restart failures mean the console is persistently broken:
    keyboard and mouse are effectively dead, which the user experiences as
    "the TUI froze". Called from the monitor thread; marshals to the app."""
    from oaset.i18n import t

    def _show() -> None:
        try:
            app.notify_error(
                RuntimeError(t("event_monitor_dead")),
                source="input",
                code="event_monitor.dead",
                hint=t("event_monitor_dead_hint"),
            )
        except Exception:
            pass  # nothing left to do but avoid crashing the shutdown path

    try:
        call_coroutine = getattr(app, "call_from_thread", None)
        if call_coroutine is not None:
            call_coroutine(_show)
        else:  # pragma: no cover - no app thread (tests)
            _show()
    except Exception:
        pass


def reapply_console_mode() -> None:
    """Re-assert the fixed console mode after an external program ran.

    `app.suspend()` hands the terminal to a child (the /view pager, Ctrl+G
    editor). Such a child — or the shell wrapper around it — can leave the
    console in a different mode than the driver expects; on resume Textual
    does not necessarily re-run enable_application_mode. Without this, keys
    or clicks silently stop working after the FIRST pager/editor use, which
    users report as "the TUI froze". Idempotent, non-Windows no-op."""
    if os.name != "nt":
        return
    try:
        from textual.drivers import win32 as win32mod

        terminal_in = sys.__stdin__
        terminal_out = sys.__stdout__
        if terminal_in is None or terminal_out is None:
            return
        out_mode = win32mod.get_console_mode(terminal_out)
        win32mod.set_console_mode(
            terminal_out, out_mode | win32mod.ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        win32mod.set_console_mode(terminal_in, FIXED_INPUT_MODE)
        _mouse_debug_log("console mode reapplied after suspend")
    except Exception:
        pass  # best effort: a broken console must not crash the resume path


def _install_console_mode_fix() -> None:
    """Replace win32.enable_application_mode with the mouse-aware variant."""
    from textual.drivers import win32 as win32mod

    def enable_application_mode_fixed() -> Callable[[], None]:
        # textual drives sys.__stdin__/__stdout__; both are Optional in typeshed.
        terminal_in = sys.__stdin__
        terminal_out = sys.__stdout__
        if terminal_in is None or terminal_out is None:  # pragma: no cover
            return lambda: None
        current_mode_in = win32mod.get_console_mode(terminal_in)
        current_mode_out = win32mod.get_console_mode(terminal_out)

        def restore() -> None:
            win32mod.set_console_mode(terminal_in, current_mode_in)
            win32mod.set_console_mode(terminal_out, current_mode_out)

        win32mod.set_console_mode(
            terminal_out, current_mode_out | win32mod.ENABLE_VIRTUAL_TERMINAL_PROCESSING
        )
        # The fix: keep MOUSE and WINDOW input enabled alongside VT input
        # (upstream resets the mode to bare ENABLE_VIRTUAL_TERMINAL_INPUT).
        win32mod.set_console_mode(terminal_in, FIXED_INPUT_MODE)
        _mouse_debug_log(
            "console mode in: was=0x%x set=0x%x" % (current_mode_in, FIXED_INPUT_MODE)
        )
        return restore

    setattr(win32mod, "enable_application_mode", enable_application_mode_fixed)


def _install_mouse_event_monitor() -> None:
    """Replace win32.EventMonitor with a MOUSE_EVENT-aware subclass."""
    from textual import events
    from textual._xterm_parser import XTermParser
    from textual.constants import DEBUG
    from textual.drivers import win32 as win32mod
    from textual.geometry import Size

    class MouseAwareEventMonitor(win32mod.EventMonitor):
        """Upstream EventMonitor + MOUSE_EVENT_RECORD translation."""

        def __init__(
            self,
            loop: asyncio.AbstractEventLoop,
            app: App,
            exit_event: threading.Event,
            process_event: Callable[[Any], None],
        ) -> None:
            super().__init__(loop, app, exit_event, process_event)
            self._mouse_tracker = MouseEventTranslator()

        def _emit_mouse(self, tuples: List[MouseTuple]) -> None:
            for kind, x, y, button, shift, meta, ctrl in tuples:
                builders = {
                    "down": events.MouseDown,
                    "up": events.MouseUp,
                    "move": events.MouseMove,
                }
                if kind in builders:
                    event = builders[kind](
                        None, x, y, 0, 0, button, shift, meta, ctrl,
                        screen_x=x, screen_y=y,
                    )
                else:
                    scroll = {
                        "wheel_up": events.MouseScrollUp,
                        "wheel_down": events.MouseScrollDown,
                        "wheel_left": events.MouseScrollLeft,
                        "wheel_right": events.MouseScrollRight,
                    }[kind]
                    event = scroll(
                        None, x, y, 0, 0, 0, shift, meta, ctrl,
                        screen_x=x, screen_y=y,
                    )
                # A poison mouse record must never take the input thread (and
                # with it the whole app) down; drop the event and keep pumping.
                try:
                    self.process_event(event)
                except Exception as error:  # noqa: BLE001
                    try:
                        self.app.log.error("mouse event dropped", error)
                    except Exception:
                        pass

        def _safe_process(self, event) -> None:
            """process_event, hardened: the key/resize path gets the same
            poison-record guarantee the mouse path has — one bad record is
            dropped and logged, never allowed to kill the monitor thread."""
            try:
                self.process_event(cast(Any, event))
            except Exception as error:  # noqa: BLE001
                try:
                    self.app.log.error("input event dropped", error)
                except Exception:
                    pass

        def run(self) -> None:
            # Bounded restart: a transient exception inside the monitor used to
            # fall out of run() and kill the THREAD — keyboard AND mouse went
            # permanently dead with no hint (P4-4). Retry with a breather so a
            # persistent error cannot hot-spin the core; give up after ~4 min.
            for _ in range(1000):
                try:
                    self._run_once()
                    return  # clean exit: the app is shutting down
                except Exception as error:
                    self.app.log.error("EVENT MONITOR ERROR", error)
                    time.sleep(0.25)
            report_event_monitor_death(self.app)

        def _run_once(self) -> None:  # noqa: C901 (mirrors upstream structure)
            exit_requested = self.exit_event.is_set
            parser = XTermParser(debug=DEBUG)

            try:
                read_count = win32mod.wintypes.DWORD(0)
                h_in = win32mod.GetStdHandle(win32mod.STD_INPUT_HANDLE)

                MAX_EVENTS = 1024
                KEY_EVENT = 0x0001
                MOUSE_EVENT = 0x0002
                WINDOW_BUFFER_SIZE_EVENT = 0x0004

                arrtype = win32mod.INPUT_RECORD * MAX_EVENTS
                input_records = arrtype()
                read_console_input_w = win32mod.KERNEL32.ReadConsoleInputW
                keys: List[str] = []
                append_key = keys.append

                def flush_keys() -> None:
                    if not keys:
                        return
                    # https://github.com/Textualize/textual/issues/3178
                    for event in parser.feed(
                        "".join(keys).encode("utf-16", "surrogatepass").decode("utf-16")
                    ):
                        # XTermParser yields Event subclasses (Key/Resize/Mouse*).
                        self._safe_process(event)
                    del keys[:]

                while not exit_requested():

                    for event in parser.tick():
                        self._safe_process(event)

                    if win32mod.wait_for_handles([h_in], 100) is None:
                        continue

                    read_console_input_w(
                        h_in, win32mod.ctypes.byref(input_records), MAX_EVENTS,
                        win32mod.ctypes.byref(read_count),
                    )
                    read_input_records = input_records[: read_count.value]

                    del keys[:]
                    new_size: Optional[tuple[int, int]] = None

                    for input_record in read_input_records:
                        event_type = input_record.EventType

                        if event_type == KEY_EVENT:
                            key_event = input_record.Event.KeyEvent
                            key = key_event.uChar.UnicodeChar
                            if key_event.bKeyDown:
                                if (
                                    key_event.dwControlKeyState
                                    and key_event.wVirtualKeyCode == 0
                                ):
                                    continue
                                append_key(key)
                        elif event_type == MOUSE_EVENT:
                            # Upstream drops these records entirely.
                            flush_keys()
                            mouse_event = input_record.Event.MouseEvent
                            _mouse_debug_log(
                                "MOUSE flags=%d btn=0x%x ctrl=0x%x at (%d,%d)"
                                % (
                                    mouse_event.dwEventFlags,
                                    mouse_event.dwButtonState,
                                    mouse_event.dwControlKeyState,
                                    mouse_event.dwMousePosition.X,
                                    mouse_event.dwMousePosition.Y,
                                )
                            )
                            self._emit_mouse(
                                self._mouse_tracker.feed(
                                    mouse_event.dwEventFlags,
                                    mouse_event.dwButtonState,
                                    mouse_event.dwControlKeyState,
                                    mouse_event.dwMousePosition.X,
                                    mouse_event.dwMousePosition.Y,
                                )
                            )
                        elif event_type == WINDOW_BUFFER_SIZE_EVENT:
                            size = input_record.Event.WindowBufferSizeEvent.dwSize
                            new_size = (size.X, size.Y)

                    flush_keys()
                    if new_size is not None:
                        self.on_size_change(*new_size)

            except Exception:
                # Restart ownership belongs to run()'s bounded retry loop.
                raise

        def on_size_change(self, width: int, height: int) -> None:
            size = Size(width, height)
            event = events.Resize(size, size)
            run_coroutine_threadsafe(self.app._post_message(event), loop=self.loop)

    setattr(win32mod, "EventMonitor", MouseAwareEventMonitor)


_applied = False


def _mouse_debug_log(text: str) -> None:
    """Optional trace when OASET_MOUSE_DEBUG=1 (kept out of normal runs)."""
    import os

    if not os.environ.get("OASET_MOUSE_DEBUG"):
        return
    try:
        from oaset.utils import oaset_home

        log = oaset_home() / "logs" / "mouse_debug.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


def apply() -> None:
    """Install both fixes. Idempotent; no-op off Windows."""
    global _applied
    if _applied or sys.platform != "win32":
        return
    from textual.drivers import win32 as win32mod

    if not hasattr(win32mod, "MOUSE_EVENT_RECORD"):
        return  # unexpected upstream shape; do nothing rather than half-patch
    _install_console_mode_fix()
    _install_mouse_event_monitor()
    setattr(win32mod, "_oaset_mouse_fix", True)
    _applied = True


apply()  # install on import; app.py only needs to import this module
