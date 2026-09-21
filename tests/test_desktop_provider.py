"""WindowsDesktopProvider (UIA) phase 1: protocol + gate + injectable backend."""

from __future__ import annotations

import pytest

from oaset.computer.desktop import (
    COORDINATE_SPACE,
    DesktopElement,
    DesktopError,
    DesktopScreenshot,
    DesktopWindow,
    GatedDesktopPage,
    UiaDesktopPage,
    _current_backend,
    _rect,
    register_desktop_backend,
)

# ------------------------------------------------------------------ fake backend

class FakeUiaBackend:
    """In-memory UIA surface: two windows, one dialog with a button + edit."""

    def __init__(self) -> None:
        self.windows = [
            DesktopWindow(handle="w-notepad", title="Untitled - Notepad", pid=1001,
                          process_name="notepad.exe",
                          rect=_rect(0, 0, 1920, 1080, scale=1.25, display=1),
                          foreground=True),
            DesktopWindow(handle="w-dialog", title="Save As", pid=1002,
                          rect=_rect(100, 100, 600, 400, scale=1.25, display=1)),
        ]
        self.elements = {
            "e-save": DesktopElement(handle="e-save", window_handle="w-dialog",
                                     control_type="Button", name="Save",
                                     rect=_rect(300, 300, 90, 28, scale=1.25, display=1)),
            "e-name": DesktopElement(handle="e-name", window_handle="w-dialog",
                                     control_type="Edit", name="File name",
                                     rect=_rect(120, 180, 380, 26, scale=1.25, display=1)),
        }
        self.clicked: list[str] = []
        self.typed: dict[str, str] = {}
        self.captures = 0

    def list_windows(self):
        return self.windows

    def find_element(self, window_handle, selector):
        el = self.elements.get(selector)
        return el if el and el.window_handle == window_handle else None

    def elements(self, window_handle):
        return [e for e in self.elements.values() if e.window_handle == window_handle]

    def click_element(self, handle):
        if handle not in self.elements:
            return False
        self.clicked.append(handle)
        return True

    def type_text(self, handle, text):
        if handle not in self.elements:
            return False
        self.typed[handle] = text
        return True

    def capture(self, window_handle):
        self.captures += 1
        return DesktopScreenshot(mime="image/png", data=b"\x89PNG-fake-desktop",
                                 width=2400, height=1350,  # 1920x1080 @1.25
                                 scale_factor=1.25, display_id=1,
                                 window_handle=window_handle)


class AllowGate:
    async def request(self, tool_name, level, summary, preview=None):
        return "always"


class DenyGate:
    async def request(self, tool_name, level, summary, preview=None):
        return "deny"


# ---------------------------------------------------------------- observation

async def test_list_windows_and_find_ungated_with_metadata():
    backend = FakeUiaBackend()
    page = GatedDesktopPage(UiaDesktopPage(backend), DenyGate())  # even deny-gate
    windows = await page.list_windows()
    assert [w.handle for w in windows] == ["w-notepad", "w-dialog"]
    rect = windows[0].rect
    assert rect["coordinate_space"] == COORDINATE_SPACE
    assert rect["scale_factor"] == 1.25 and rect["display_id"] == 1

    el = await page.find("w-dialog", "e-save")
    assert el is not None and el.control_type == "Button"
    assert el.rect["coordinate_space"] == COORDINATE_SPACE
    assert await page.find("w-notepad", "e-save") is None  # wrong window


# ---------------------------------------------------------------- gate: allow

async def test_click_type_screenshot_allowed_via_gate():
    backend = FakeUiaBackend()
    page = GatedDesktopPage(UiaDesktopPage(backend), AllowGate())
    assert await page.click("e-save") is True
    assert backend.clicked == ["e-save"]
    assert await page.type_text("e-name", "report.txt") is True
    assert backend.typed["e-name"] == "report.txt"
    shot = await page.screenshot("w-dialog")
    assert shot.data.startswith(b"\x89PNG")
    assert backend.captures == 1
    # §5.3 metadata travels with the shot
    meta = shot.metadata()
    assert meta["coordinate_space"] == COORDINATE_SPACE
    assert meta["scale_factor"] == 1.25 and meta["display_id"] == 1
    assert meta["window_handle"] == "w-dialog"
    assert meta["captured_at"] > 0


# ----------------------------------------------------------------- gate: deny

async def test_gate_denial_leaves_backend_untouched():
    backend = FakeUiaBackend()
    page = GatedDesktopPage(UiaDesktopPage(backend), DenyGate())
    with pytest.raises(DesktopError) as excinfo:
        await page.click("e-save")
    assert excinfo.value.code == "permission_denied"
    with pytest.raises(DesktopError):
        await page.type_text("e-name", "nope")
    with pytest.raises(DesktopError):
        await page.screenshot()
    assert backend.clicked == [] and backend.typed == {} and backend.captures == 0


async def test_stale_handle_returns_false():
    backend = FakeUiaBackend()
    page = GatedDesktopPage(UiaDesktopPage(backend), AllowGate())
    assert await page.click("e-gone") is False
    assert await page.type_text("e-gone", "x") is False


async def test_missing_backend_is_honest_not_fake():
    page = GatedDesktopPage(UiaDesktopPage(None), AllowGate())
    with pytest.raises(DesktopError) as excinfo:
        await page.list_windows()
    assert excinfo.value.code == "backend_unavailable"
    assert "UIA" in str(excinfo.value)


# ------------------------------------------------------------- registry seam

async def test_backend_registry_seam():
    register_desktop_backend(FakeUiaBackend)
    try:
        backend = _current_backend()
        assert backend is not None
        page = GatedDesktopPage(UiaDesktopPage(backend), AllowGate())
        assert len(await page.list_windows()) == 2
    finally:
        register_desktop_backend(lambda: None)  # reset to unavailable
