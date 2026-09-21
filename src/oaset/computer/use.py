"""Local computer-use session: protocol-compatible actions on Windows.

The model speaks screenshot-space coordinates (the scaled PNG we send it).
The session converts those to physical pixels, drives InputDriver, and
returns PNG bytes the agent loop can attach as a tool-result image.

UI-TARS / OpenCUA / CUA all do screenshot → action → screenshot. This is
the same loop, executed by oAset's UIA capture + SendInput — not by wrapping
those projects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from oaset.computer.desktop import DesktopError, DesktopScreenshot
from oaset.computer.image import (
    crop_screenshot_to_png,
    scale_down,
    scale_up,
    screenshot_frame,
    screenshot_to_png,
)
from oaset.computer.input import FakeInputDriver, InputDriver, default_driver, pause

ACTIONS = frozenset({
    "key", "type", "mouse_move", "left_click", "left_click_drag",
    "right_click", "middle_click", "double_click", "triple_click",
    "screenshot", "cursor_position",
    "left_mouse_down", "left_mouse_up", "scroll", "hold_key", "wait", "zoom",
})

_CLICK_BUTTON = {
    "left_click": ("left", 1),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}

_SCROLL_DELTA = {"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0)}


@dataclass
class UseResult:
    output: str = ""
    error: str = ""
    png: bytes | None = None
    width: int = 0
    height: int = 0

    @property
    def is_error(self) -> bool:
        return bool(self.error)


class ComputerUseSession:
    """One session's computer-use state: last capture, frame size, driver."""

    def __init__(self, backend: Any, driver: InputDriver | None = None, gate: Any = None):
        self.backend = backend
        self.driver = driver or default_driver()
        self.gate = gate
        self._last: DesktopScreenshot | None = None
        self._frame: tuple[int, int] = (1280, 800)
        self._physical: tuple[int, int] = self.driver.display_size()
        self._mouse_down = False

    @property
    def frame(self) -> tuple[int, int]:
        return self._frame

    @property
    def physical(self) -> tuple[int, int]:
        return self._physical

    def options(self) -> dict[str, int]:
        """Values the computer-use tool declaration expects (screenshot pixels)."""
        return {"display_width_px": self._frame[0], "display_height_px": self._frame[1]}

    async def _approve(self, action: str, summary: str, preview: str | None = None) -> None:
        if self.gate is None:
            return
        decision = await self.gate.request(f"computer_{action}", "exec", summary, preview)
        if not decision or str(decision).lower() in ("deny", "denied"):
            raise DesktopError("permission_denied", f"computer {action} denied")

    def _capture_raw(self) -> DesktopScreenshot:
        if self.backend is None:
            raise DesktopError("backend_unavailable", "no desktop capture backend")
        shot = self.backend.capture(None)
        self._last = shot
        self._physical = (shot.width, shot.height)
        self._frame = screenshot_frame(shot.width, shot.height)
        return shot

    def _png_of(self, shot: DesktopScreenshot) -> tuple[bytes, int, int]:
        return screenshot_to_png(shot.mime, shot.data, shot.width, shot.height, self._frame)

    def _to_physical(self, coordinate) -> tuple[int, int]:
        if not isinstance(coordinate, (list, tuple)) or len(coordinate) != 2:
            raise DesktopError("bad_coordinate", f"{coordinate} must be [x, y]")
        x, y = int(coordinate[0]), int(coordinate[1])
        if x < 0 or y < 0:
            raise DesktopError("bad_coordinate", f"{coordinate} must be non-negative")
        return scale_up(x, y, self._frame, self._physical)

    async def screenshot(self) -> UseResult:
        await self._approve("screenshot", "Capture the desktop (computer-use)")
        shot = self._capture_raw()
        png, w, h = self._png_of(shot)
        return UseResult(output=f"screenshot {w}x{h}", png=png, width=w, height=h)

    async def act(self, args: dict[str, Any]) -> UseResult:
        action = str(args.get("action") or "").strip()
        if not action:
            # toolset form: the tool name IS the action (computer_toolset members)
            action = str(args.get("_member") or "").strip()
        if action not in ACTIONS:
            return UseResult(error=f"unknown computer action {action!r}")
        try:
            return await self._act(action, args)
        except DesktopError as exc:
            return UseResult(error=f"[{exc.code}] {exc}")

    async def _act(self, action: str, args: dict[str, Any]) -> UseResult:
        if action == "screenshot":
            return await self.screenshot()
        if action == "wait":
            duration = float(args.get("duration") or 0)
            pause(duration)
            return await self.screenshot()
        if action == "cursor_position":
            px, py = self.driver.cursor()
            sx, sy = scale_down(px, py, self._frame, self._physical)
            return UseResult(output=f"X={sx},Y={sy}")
        if action == "zoom":
            return await self._zoom(args.get("region"))
        if action == "key":
            text = args.get("text")
            if not text:
                return UseResult(error="text is required for key")
            await self._approve("key", f"Press key {text}", preview=str(text))
            self.driver.key(str(text), repeat=int(args.get("repeat") or 1))
            return await self.screenshot()
        if action == "hold_key":
            text = args.get("text")
            if not text:
                return UseResult(error="text is required for hold_key")
            duration = float(args.get("duration") or 0)
            await self._approve("hold_key", f"Hold {text} for {duration}s", preview=str(text))
            self.driver.key_hold(str(text), True)
            pause(duration)
            self.driver.key_hold(str(text), False)
            return await self.screenshot()
        if action == "type":
            text = args.get("text")
            if text is None:
                return UseResult(error="text is required for type")
            await self._approve("type", f"Type {len(str(text))} chars", preview=str(text)[:200])
            self.driver.type_text(str(text))
            return await self.screenshot()
        if action == "mouse_move":
            x, y = self._to_physical(args.get("coordinate"))
            await self._approve("mouse_move", f"Move pointer to ({x},{y})")
            self.driver.move(x, y)
            return await self.screenshot()
        if action == "left_mouse_down":
            await self._approve("left_mouse_down", "Press left mouse button")
            self.driver.button("left", True)
            self._mouse_down = True
            return UseResult(output="left down")
        if action == "left_mouse_up":
            await self._approve("left_mouse_up", "Release left mouse button")
            self.driver.button("left", False)
            self._mouse_down = False
            return await self.screenshot()
        if action == "left_click_drag":
            start = args.get("start_coordinate")
            end = args.get("coordinate")
            sx, sy = self._to_physical(start)
            ex, ey = self._to_physical(end)
            await self._approve("drag", f"Drag ({sx},{sy}) → ({ex},{ey})")
            chord = args.get("text") or args.get("key")
            if chord:
                self.driver.key_hold(str(chord), True)
            self.driver.move(sx, sy)
            self.driver.button("left", True)
            self.driver.move(ex, ey)
            self.driver.button("left", False)
            if chord:
                self.driver.key_hold(str(chord), False)
            return await self.screenshot()
        if action == "scroll":
            direction = str(args.get("scroll_direction") or "down").lower()
            delta = _SCROLL_DELTA.get(direction)
            if delta is None:
                return UseResult(error=f"scroll_direction must be up/down/left/right, not {direction}")
            amount = int(args.get("scroll_amount") or 1)
            if args.get("coordinate") is not None:
                x, y = self._to_physical(args["coordinate"])
                self.driver.move(x, y)
            await self._approve("scroll", f"Scroll {direction} ×{amount}")
            self.driver.scroll(delta[0] * amount, delta[1] * amount)
            return await self.screenshot()
        if action in _CLICK_BUTTON:
            button, count = _CLICK_BUTTON[action]
            if args.get("coordinate") is not None:
                x, y = self._to_physical(args["coordinate"])
                self.driver.move(x, y)
            chord = args.get("key") or args.get("text")
            await self._approve(action, f"{action} at pointer", preview=str(args.get("coordinate")))
            if chord:
                self.driver.key_hold(str(chord), True)
            self.driver.click(button, count)
            if chord:
                self.driver.key_hold(str(chord), False)
            return await self.screenshot()
        return UseResult(error=f"unhandled action {action}")

    async def _zoom(self, region) -> UseResult:
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            return UseResult(error="region must be [x0, y0, x1, y1] in screenshot pixels")
        await self._approve("zoom", f"Zoom region {list(region)}")
        shot = self._last or self._capture_raw()
        png, w, h = self._png_of(shot)
        try:
            zoomed = crop_screenshot_to_png(
                shot.mime, shot.data, shot.width, shot.height,
                (int(region[0]), int(region[1]), int(region[2]), int(region[3])),
                self._frame,
            )
        except ValueError as exc:
            return UseResult(error=str(exc), png=png, width=w, height=h)
        return UseResult(output=f"zoom {list(region)}", png=zoomed, width=self._frame[0],
                         height=self._frame[1])


def session_for(ctx) -> ComputerUseSession:
    """Cached on ToolContext.session_state so consecutive actions share the frame."""
    existing = ctx.session_state.get("computer_use")
    if isinstance(existing, ComputerUseSession):
        return existing
    backend = ctx.session_state.get("desktop_backend")
    if backend is None:
        from oaset.computer.desktop import _current_backend
        backend = _current_backend()
    driver = ctx.session_state.get("input_driver")
    if driver is None:
        # tests inject a Fake* backend: never SendInput against the real desktop
        name = type(backend).__name__ if backend is not None else ""
        if name.startswith("Fake"):
            width, height = 1920, 1080
            windows = getattr(backend, "windows", None) or []
            if windows:
                rect = windows[0].rect or {}
                width = int(rect.get("w") or 1920)
                height = int(rect.get("h") or 1080)
            driver = FakeInputDriver(width=width, height=height)
    session = ComputerUseSession(backend, driver=driver, gate=getattr(ctx, "gate", None))
    ctx.session_state["computer_use"] = session
    return session
