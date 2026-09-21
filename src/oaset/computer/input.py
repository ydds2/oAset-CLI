"""Pointer + keyboard driver for local computer-use.

Windows: SendInput (absolute mouse, wheel, Unicode + virtual-key chords).
Tests inject a FakeInputDriver that records actions and never touches the OS.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from oaset.computer.desktop import DesktopError

_VK = {
    "return": 0x0D, "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08, "space": 0x20, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27,
    "down": 0x28, "insert": 0x2D,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "super": 0x5B, "meta": 0x5B, "cmd": 0x5B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}
for _i, _ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _VK[_ch] = 0x41 + _i
for _i in range(10):
    _VK[str(_i)] = 0x30 + _i

_MODIFIERS = {"ctrl", "control", "alt", "shift", "win", "super", "meta", "cmd"}

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
WHEEL_DELTA = 120
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
SM_CXSCREEN = 0
SM_CYSCREEN = 1

_BUTTON_FLAGS = {
    ("left", True): MOUSEEVENTF_LEFTDOWN,
    ("left", False): MOUSEEVENTF_LEFTUP,
    ("right", True): MOUSEEVENTF_RIGHTDOWN,
    ("right", False): MOUSEEVENTF_RIGHTUP,
    ("middle", True): MOUSEEVENTF_MIDDLEDOWN,
    ("middle", False): MOUSEEVENTF_MIDDLEUP,
}


class InputDriver(Protocol):
    def display_size(self) -> tuple[int, int]: ...
    def move(self, x: int, y: int) -> None: ...
    def button(self, name: str, down: bool) -> None: ...
    def click(self, name: str, count: int = 1) -> None: ...
    def scroll(self, dx: int, dy: int) -> None: ...
    def key(self, chord: str, repeat: int = 1) -> None: ...
    def key_hold(self, chord: str, down: bool) -> None: ...
    def type_text(self, text: str) -> None: ...
    def cursor(self) -> tuple[int, int]: ...


@dataclass
class FakeInputDriver:
    """Records actions; never talks to the OS. Default display 1920×1080."""

    width: int = 1920
    height: int = 1080
    x: int = 0
    y: int = 0
    log: list[tuple] = field(default_factory=list)

    def display_size(self) -> tuple[int, int]:
        return self.width, self.height

    def move(self, x: int, y: int) -> None:
        self.x, self.y = int(x), int(y)
        self.log.append(("move", self.x, self.y))

    def button(self, name: str, down: bool) -> None:
        self.log.append(("button", name, "down" if down else "up"))

    def click(self, name: str, count: int = 1) -> None:
        self.log.append(("click", name, count, self.x, self.y))

    def scroll(self, dx: int, dy: int) -> None:
        self.log.append(("scroll", dx, dy, self.x, self.y))

    def key(self, chord: str, repeat: int = 1) -> None:
        self.log.append(("key", chord, repeat))

    def key_hold(self, chord: str, down: bool) -> None:
        self.log.append(("key_hold", chord, "down" if down else "up"))

    def type_text(self, text: str) -> None:
        self.log.append(("type", text))

    def cursor(self) -> tuple[int, int]:
        return self.x, self.y


def _split_chord(chord: str) -> list[str]:
    parts = [p.strip().lower() for p in chord.replace("-", "+").split("+") if p.strip()]
    return parts or [chord.lower()]


_WIN: tuple[Any, Any, Any, Any, Any] | None = None


def _win_structs():
    """ctypes INPUT layout, built once — SendInput is on the hot path."""
    global _WIN
    if _WIN is not None:
        return _WIN
    import ctypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.c_void_p)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                    ("wParamH", ctypes.c_ushort)]

    class UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", UNION)]

    _WIN = (ctypes, MOUSEINPUT, KEYBDINPUT, UNION, INPUT)
    return _WIN


class WindowsInputDriver:
    """SendInput against the interactive desktop. Physical-pixel coordinates."""

    def display_size(self) -> tuple[int, int]:
        cached = getattr(self, "_size", None)
        if cached is not None:
            return cached
        import ctypes
        user32 = ctypes.windll.user32
        self._size = (int(user32.GetSystemMetrics(SM_CXSCREEN)),
                      int(user32.GetSystemMetrics(SM_CYSCREEN)))
        return self._size

    def _send(self, inputs: list) -> None:
        ctypes, _mi, _ki, _un, INPUT = _win_structs()
        arr = (INPUT * len(inputs))(*inputs)
        sent = ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            raise DesktopError("vision_failed", f"SendInput delivered {sent}/{len(inputs)}")

    def _mouse(self, flags: int, x: int = 0, y: int = 0, data: int = 0):
        _ct, MOUSEINPUT, _ki, UNION, INPUT = _win_structs()
        inp = INPUT(type=INPUT_MOUSE)
        inp.union.mi = MOUSEINPUT(dx=x, dy=y, mouseData=data, dwFlags=flags,
                                  time=0, dwExtraInfo=None)
        return inp

    def _key(self, vk: int, up: bool = False, unicode_scan: int | None = None):
        _ct, _mi, KEYBDINPUT, UNION, INPUT = _win_structs()
        flags = KEYEVENTF_KEYUP if up else 0
        inp = INPUT(type=INPUT_KEYBOARD)
        if unicode_scan is not None:
            flags |= KEYEVENTF_UNICODE
            inp.union.ki = KEYBDINPUT(wVk=0, wScan=unicode_scan, dwFlags=flags,
                                      time=0, dwExtraInfo=None)
        else:
            inp.union.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags,
                                      time=0, dwExtraInfo=None)
        return inp

    def _abs(self, x: int, y: int) -> tuple[int, int]:
        w, h = self.display_size()
        w = max(w, 2)
        h = max(h, 2)
        return int(x * 65535 / (w - 1)), int(y * 65535 / (h - 1))

    def move(self, x: int, y: int) -> None:
        ax, ay = self._abs(int(x), int(y))
        self._send([self._mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, ax, ay)])

    def button(self, name: str, down: bool) -> None:
        flags = _BUTTON_FLAGS.get((name, down))
        if flags is None:
            raise DesktopError("vision_failed", f"unknown button {name}")
        self._send([self._mouse(flags)])

    def click(self, name: str, count: int = 1) -> None:
        for _ in range(max(1, count)):
            self.button(name, True)
            self.button(name, False)

    def scroll(self, dx: int, dy: int) -> None:
        inputs = []
        if dy:
            inputs.append(self._mouse(MOUSEEVENTF_WHEEL, data=int(dy) * WHEEL_DELTA))
        if dx:
            inputs.append(self._mouse(MOUSEEVENTF_HWHEEL, data=int(dx) * WHEEL_DELTA))
        if inputs:
            self._send(inputs)

    def _vk(self, token: str) -> int:
        if token in _VK:
            return _VK[token]
        if len(token) == 1:
            return _VK.get(token.lower()) or ord(token.upper())
        raise DesktopError("vision_failed", f"unknown key {token!r}")

    def key(self, chord: str, repeat: int = 1) -> None:
        parts = _split_chord(chord)
        mods = [p for p in parts if p in _MODIFIERS]
        keys = [p for p in parts if p not in _MODIFIERS] or parts[-1:]
        for _ in range(max(1, min(repeat, 100))):
            seq = []
            for m in mods:
                seq.append(self._key(self._vk(m), up=False))
            for k in keys:
                seq.append(self._key(self._vk(k), up=False))
            for k in reversed(keys):
                seq.append(self._key(self._vk(k), up=True))
            for m in reversed(mods):
                seq.append(self._key(self._vk(m), up=True))
            self._send(seq)

    def key_hold(self, chord: str, down: bool) -> None:
        parts = _split_chord(chord)
        ordered = parts if down else list(reversed(parts))
        self._send([self._key(self._vk(p), up=not down) for p in ordered])

    def type_text(self, text: str) -> None:
        seq = []
        for ch in text:
            if ch in ("\r", "\n"):
                seq.append(self._key(0x0D, up=False))
                seq.append(self._key(0x0D, up=True))
                continue
            seq.append(self._key(0, unicode_scan=ord(ch)))
            seq.append(self._key(0, up=True, unicode_scan=ord(ch)))
        if seq:
            self._send(seq)

    def cursor(self) -> tuple[int, int]:
        import ctypes
        from ctypes import wintypes as wt
        pt = wt.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return int(pt.x), int(pt.y)


def default_driver() -> InputDriver:
    if sys.platform == "win32":
        return WindowsInputDriver()
    return FakeInputDriver()


def pause(seconds: float) -> None:
    time.sleep(max(0.0, min(float(seconds), 30.0)))
