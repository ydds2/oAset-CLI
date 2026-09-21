"""VisionFallbackProvider — screenshot + coordinate fallback for when the
UIA semantic tree is unavailable (plan §5.3, DESK-01).

HIGH-RISK by design: every action is visually-scoped coordinate/keyboard
input into WHATEVER window currently has focus, so

- every single action calls the unified PermissionGate per invocation
  (UIGate asks the user each time; AutoGate means the user accepted blanket
  approval via --yolo; ReadOnlyGate denies);
- summaries carry a "⚠ VISION" marker so approval panels render the risk;
- click_at validates the coordinate metadata against the screenshot it came
  from (coordinate_space, scale_factor) — no bare (x, y) accepted;
- typed text goes through SendInput KEYEVENTF_UNICODE into the focused
  control; there is no targeting, so callers must verify focus first.

Desktop actions perform no network I/O; NetworkPolicy is not consulted here
(see computer/desktop.py for that decision and its rationale).
"""

from __future__ import annotations

import asyncio
import ctypes
import time
from typing import Any

from oaset.computer.desktop import DesktopError, DesktopScreenshot
from oaset.i18n import t

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTUNION)]


def _send_mouse_at(x: int, y: int) -> None:
    user32 = ctypes.windll.user32
    if not user32.SetCursorPos(int(x), int(y)):
        raise DesktopError("vision_failed", f"SetCursorPos({x},{y}) failed")
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)


def _send_unicode(text: str) -> None:
    user32 = ctypes.windll.user32
    inputs = []

    def add_key(vk: int, scan: int, flags: int) -> None:
        inp = _INPUT(type=INPUT_KEYBOARD)
        inp.union.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags,
                                    time=0, dwExtraInfo=None)
        inputs.append(inp)

    for ch in text:
        if ch in ("\r", "\n"):
            # control keys as real VK sequences: UNICODE \r does not trigger
            # dialog default buttons (verified against a live MessageBox)
            for vk, scan in ((0x0D, 0x1C),):  # VK_RETURN
                add_key(vk, scan, 0)
                add_key(vk, scan, KEYEVENTF_KEYUP)
            continue
        scan = ord(ch)
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            add_key(0, scan, flags)
    if not inputs:
        return
    arr = (_INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        raise DesktopError("vision_failed",
                           f"SendInput delivered {sent}/{len(inputs)} events")


class VisionFallbackPage:
    """Coordinate/keyboard fallback over a capture-capable backend.

    Provenance binding (§5.3): click_at/type_text validate coordinates against
    the most recent capture — the frame must still be fresh (age < TTL), the
    point must fall inside the captured viewport, and the target window must
    still be listed by the backend (identity recheck). Screens taken without
    a prior observation are refused for input actions.
    """

    FRAME_TTL_SECONDS = 30.0

    def __init__(self, backend: Any, gate: Any, log: Any = None):
        self.backend = backend
        self.gate = gate
        self._log = log
        self._last_shot: DesktopScreenshot | None = None
        self._last_window: str | None = None
        self._last_origin: tuple[int, int] | None = None
        self._last_capture_ts: float = 0.0

    def _audit(self, detail: str) -> None:
        if self._log:
            self._log(f"vision: {detail}")

    async def _approve(self, action: str, summary: str, preview: str | None = None) -> None:
        decision = await self.gate.request(f"vision_{action}", "exec", summary, preview)
        if not decision or str(decision).lower() in ("deny", "denied"):
            raise DesktopError("permission_denied", f"vision {action} denied by user/policy")

    async def screenshot(self, window_handle: str | None = None) -> DesktopScreenshot:
        await self._approve("screenshot",
                            t("vis_screenshot")
                            + (f" of window {window_handle}" if window_handle else ""))
        shot = self.backend.capture(window_handle)
        if hasattr(shot, "__await__"):  # async backends
            shot = await shot
        origin = None
        if window_handle and hasattr(self.backend, "window_origin"):
            origin = self.backend.window_origin(window_handle)
        self._last_shot = shot
        self._last_window = window_handle
        self._last_origin = origin
        self._last_capture_ts = time.monotonic()
        self._audit(f"screenshot {shot.summary()} origin={origin}")
        return shot

    def _check_provenance(self, x: int, y: int, scale_factor: float,
                          display_id: int) -> None:
        """Bind coordinates to the last captured frame before acting.

        - a fresh observation must exist (age < FRAME_TTL_SECONDS);
        - logical coordinates are converted to physical pixels using the
          capture's own scale_factor;
        - the converted point must land inside the captured viewport.
        """
        if self._last_shot is None:
            raise DesktopError("stale_frame", "vision action requires a screenshot first")
        if time.monotonic() - self._last_capture_ts > self.FRAME_TTL_SECONDS:
            raise DesktopError("stale_frame", "observation expired; re-capture before acting")
        px = int(x * scale_factor / self._last_shot.scale_factor)
        py = int(y * scale_factor / self._last_shot.scale_factor)
        origin_x, origin_y = self._last_origin or (0, 0)
        frame_x, frame_y = px - origin_x, py - origin_y
        if not (0 <= frame_x < self._last_shot.width and 0 <= frame_y < self._last_shot.height):
            raise DesktopError(
                "stale_frame",
                f"coordinate ({frame_x},{frame_y}) outside captured viewport "
                f"{self._last_shot.width}x{self._last_shot.height}")
        self._audit(f"provenance ok: screen ({x},{y}) -> frame ({frame_x},{frame_y}) "
                    f"@{self._last_shot.scale_factor}x")
        return None

    async def click_at(self, x: int, y: int, *, scale_factor: float = 1.0,
                       display_id: int = 0, window_handle: str | None = None) -> bool:
        """Click at logical coordinates read from the LAST VISION screenshot.

        Provenance (§5.3): the point is converted with the frame's own
        scale_factor, bounds-checked against the captured viewport, and the
        target window identity is re-verified when a window_handle is given.
        Bare coordinates without a fresh observation are refused.
        """
        await self._approve(
            "click",
            t("vis_click", x=x, y=y, display_id=display_id, scale=scale_factor)
            + (t("vis_click_window", handle=window_handle) if window_handle else ""),
            preview=f"({x},{y})")
        if self._last_shot is None:
            raise DesktopError("stale_frame", "vision click requires a screenshot first")
        self._check_provenance(x, y, scale_factor, display_id)
        # window identity recheck: the target window must still be enumerated
        if window_handle and hasattr(self.backend, "list_windows"):
            listed = {w.handle for w in self.backend.list_windows()}
            if window_handle not in listed:
                raise DesktopError("stale_frame",
                                   f"target window {window_handle} no longer exists")
        px = int(x * scale_factor / self._last_shot.scale_factor)
        py = int(y * scale_factor / self._last_shot.scale_factor)
        try:
            _send_mouse_at(px, py)
        except DesktopError:
            raise
        self._audit(f"click physical ({px},{py}) display={display_id}")
        return True

    async def type_text(self, text: str) -> bool:
        """Unicode keyboard input into the CURRENTLY FOCUSED control."""
        await self._approve("type_text",
                            t("vis_type", count=len(text)),
                            preview=text[:120])
        try:
            _send_unicode(text)
        except DesktopError:
            raise
        self._audit(f"type_text ({len(text)} chars)")
        return True

    async def wait(self, seconds: float) -> None:
        """Give UI changes time to settle between vision steps."""
        await asyncio.sleep(max(0.0, min(seconds, 30.0)))

    async def close(self) -> None:
        return None
