"""WindowsDesktopProvider (UIA) — phase 1.

Desktop automation shares ONLY the base package with the browser provider:
separate selectors (UIA elements, not DOM), separate coordinate space
(virtual-screen pixels, not viewport CSS), separate permission summary text.

Security contract (see git history: docs/ARCHITECTURE.zh-CN.md §5.3, deleted 2026-09-13):

- The desktop provider performs NO network I/O itself. NetworkPolicy is
  consulted for consistency but deliberately does not gate local UIA —
  blocking a local screen read under local_only would be a lie about what
  was blocked. Egress happens in the automated apps, not here.
- EVERY screenshot, click and keystroke goes through the unified
  PermissionGate (UIGate asks, AutoGate allows under --yolo, ReadOnlyGate
  denies) — default is per-action confirmation, per the plan.
- list_windows / find are read-only observation: logged, not gated
  (mirrors how the registry auto-allows READ tools inside the workspace).
- Coordinate metadata travels WITH every rect and screenshot:
  coordinate_space="virtual_screen_pixels", display id, pixel size,
  scale_factor (DPI), timestamp — never a bare (x, y).

The UIA backend is INJECTABLE. Phase 1 ships the protocol, the gate wrapper
and tests over a fake backend; a real ctypes/COM UIA backend slots into
`UiaBackend` later without touching the gate or type layer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from oaset.i18n import t

COORDINATE_SPACE = "virtual_screen_pixels"


class DesktopError(Exception):
    """Structured desktop failure; `code` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _rect(x: int, y: int, w: int, h: int, scale: float, display: int) -> dict[str, Any]:
    """Rect + mandatory coordinate metadata (§5.3: no bare x/y)."""
    return {"x": x, "y": y, "w": w, "h": h,
            "scale_factor": scale, "display_id": display,
            "coordinate_space": COORDINATE_SPACE}


@dataclass(frozen=True)
class DesktopWindow:
    handle: str
    title: str
    pid: int = 0
    process_name: str = ""
    rect: dict[str, Any] = field(default_factory=dict)
    foreground: bool = False


@dataclass(frozen=True)
class DesktopElement:
    """One UIA node: `handle` is the stable reference for click/type_text."""

    handle: str
    window_handle: str
    control_type: str            # Button / Edit / Document / ...
    name: str = ""
    value: str = ""
    rect: dict[str, Any] = field(default_factory=dict)
    actionable: bool = True      # False for pure containers


@dataclass(frozen=True)
class DesktopScreenshot:
    mime: str
    data: bytes
    width: int
    height: int
    scale_factor: float = 1.0
    display_id: int = 0
    window_handle: str | None = None
    captured_at: float = field(default_factory=time.time)

    def metadata(self) -> dict[str, Any]:
        """§5.3 screenshot metadata block (coordinates stay checkable)."""
        return {"coordinate_space": COORDINATE_SPACE,
                "display_id": self.display_id,
                "width": self.width, "height": self.height,
                "scale_factor": self.scale_factor,
                "window_handle": self.window_handle,
                "captured_at": self.captured_at}

    def summary(self) -> str:
        return (f"{self.mime} {self.width}x{self.height} @{self.scale_factor}x "
                f"display={self.display_id}")


class UiaBackend(Protocol):
    """Injectable Windows UIA surface. The real backend implements this with
    COM/UIA; tests inject a fake. Page-level semantics live above this."""

    def list_windows(self) -> list[DesktopWindow]: ...
    def find_element(self, window_handle: str, selector: str) -> DesktopElement | None: ...
    def elements(self, window_handle: str) -> list[DesktopElement]: ...
    def click_element(self, handle: str) -> bool: ...
    def type_text(self, handle: str, text: str) -> bool: ...
    def capture(self, window_handle: str | None) -> DesktopScreenshot: ...


class DesktopPage(Protocol):
    async def list_windows(self) -> list[DesktopWindow]: ...
    async def find(self, window_handle: str, selector: str) -> DesktopElement | None: ...
    async def click(self, handle: str) -> bool: ...
    async def type_text(self, handle: str, text: str) -> bool: ...
    async def screenshot(self, window_handle: str | None = None) -> DesktopScreenshot: ...
    async def close(self) -> None: ...


class UiaDesktopPage:
    """DesktopPage over an injected UiaBackend (sync backend, async surface)."""

    def __init__(self, backend: UiaBackend | None):
        self.backend = backend

    def _require_backend(self) -> UiaBackend:
        if self.backend is None:
            raise DesktopError(
                "backend_unavailable",
                t("desk_no_backend"))
        return self.backend

    async def list_windows(self) -> list[DesktopWindow]:
        return self._require_backend().list_windows()

    async def find(self, window_handle: str, selector: str) -> DesktopElement | None:
        return self._require_backend().find_element(window_handle, selector)

    async def click(self, handle: str) -> bool:
        return self._require_backend().click_element(handle)

    async def type_text(self, handle: str, text: str) -> bool:
        return self._require_backend().type_text(handle, text)

    async def screenshot(self, window_handle: str | None = None) -> DesktopScreenshot:
        return self._require_backend().capture(window_handle)

    async def close(self) -> None:
        self.backend = None


class GatedDesktopPage:
    """Wrap any DesktopPage with per-action approval + audit logging.

    Gate semantics mirror the tool registry: click/type_text are exec-class,
    screenshot is read-class but confirmed per action by default (it can
    capture sensitive content), list_windows/find are ungated observation.
    """

    def __init__(self, page: DesktopPage, gate: Any, policy: Any = None, log: Any = None):
        self.page = page
        self.gate = gate
        self.policy = policy  # consulted for reporting only — see module docstring
        self._log = log

    def _audit(self, action: str, detail: str) -> None:
        if self._log:
            self._log(f"desktop {action}: {detail}")

    async def _approve(self, action: str, level: str, summary: str, preview: str | None = None) -> None:
        decision = await self.gate.request(f"desktop_{action}", level, summary, preview)
        if not decision or str(decision).lower() in ("deny", "denied"):
            raise DesktopError("permission_denied", f"desktop {action} denied by user/policy")

    async def list_windows(self) -> list[DesktopWindow]:
        windows = await self.page.list_windows()
        self._audit("list_windows", f"{len(windows)} window(s)")
        return windows

    async def find(self, window_handle: str, selector: str) -> DesktopElement | None:
        element = await self.page.find(window_handle, selector)
        self._audit("find", f"{selector!r} -> "
                    f"{element.handle if element else 'None'}")
        return element

    async def click(self, handle: str) -> bool:
        await self._approve("click", "exec", f"Click desktop element {handle}", preview=handle)
        result = await self.page.click(handle)
        self._audit("click", f"{handle} -> {result}")
        return result

    async def type_text(self, handle: str, text: str) -> bool:
        preview = text if len(text) <= 200 else text[:200] + "…"
        await self._approve("type_text", "exec",
                            f"Type {len(text)} chars into desktop element {handle}",
                            preview=preview)
        result = await self.page.type_text(handle, text)
        self._audit("type_text", f"{handle} ({len(text)} chars) -> {result}")
        return result

    async def screenshot(self, window_handle: str | None = None) -> DesktopScreenshot:
        await self._approve("screenshot", "read",
                            "Capture a desktop screenshot"
                            + (f" of window {window_handle}" if window_handle else ""))
        shot = await self.page.screenshot(window_handle)
        self._audit("screenshot", shot.summary())
        return shot

    async def close(self) -> None:
        await self.page.close()


# Backend registry seam: the real UIA backend registers itself here when
# available; until then /desktop reports honest unavailability.
_backend_factory = None


def register_desktop_backend(factory) -> None:
    """Register a () -> UiaBackend factory (real UIA wiring, or tests)."""
    global _backend_factory
    _backend_factory = factory


def _current_backend() -> UiaBackend | None:
    if _backend_factory is None:
        return None
    try:
        return _backend_factory()
    except Exception:
        return None
