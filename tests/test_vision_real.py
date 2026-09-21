"""REAL-machine VisionFallbackProvider verification (Windows only).

The fallback is exercised end-to-end with real screen effects:
- vision click_at at the coordinates of a REAL MessageBox OK button
  (coordinates sourced from the UIA semantic layer in this test, which is
  exactly how the fallback is meant to be driven) closes the box;
- vision type_text of Enter closes a REAL MessageBox;
- DenyGate refuses and the box stays open.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest


def _interactive_desktop_available() -> bool:
    """OpenInputDesktop returns NULL when the workstation is locked — real-screen
    input tests need the interactive console; a locked run is an environment
    skip, not a product regression.

    Non-Windows has no user32: the module must still import during collection
    on Linux CI, and the tests are skipped there anyway.
    """
    import ctypes
    import sys

    if sys.platform != "win32":
        return False

    user32 = ctypes.windll.user32
    handle = user32.OpenInputDesktop(0, False, 0x02)
    if handle:
        user32.CloseDesktop(handle)
        return True
    return False


@pytest.fixture(autouse=True)
def _require_interactive_desktop():
    """Real-screen tests need the interactive console; if the workstation
    locks mid-run, subsequent tests skip instead of failing."""
    if not _interactive_desktop_available():
        pytest.skip("interactive desktop unavailable (workstation locked)")


from oaset.computer.desktop import DesktopError  # noqa: E402
from oaset.computer.uia_backend import UiaComBackend  # noqa: E402
from oaset.computer.vision import VisionFallbackPage  # noqa: E402

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="real-machine input suite"),
    pytest.mark.skipif(not _interactive_desktop_available(),
                       reason="interactive desktop unavailable (workstation locked)"),
    pytest.mark.real_input,
]


class AllowGate:
    async def request(self, tool_name, level, summary, preview=None):
        return "always"


class DenyGate:
    async def request(self, tool_name, level, summary, preview=None):
        return "deny"


def _wait_window(backend, title_prefix, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for window in backend.list_windows():
            if window.title.startswith(title_prefix):
                return window
        time.sleep(0.15)
    return None


def test_real_vision_click_closes_message_box():
    if not _interactive_desktop_available():
        pytest.skip("interactive desktop unavailable (workstation locked)")
    backend = UiaComBackend()
    vision = VisionFallbackPage(backend, AllowGate())
    child = subprocess.Popen([
        sys.executable, "-c",
        "import ctypes,sys; sys.exit(0 if ctypes.windll.user32.MessageBoxW("
        "0, 'body', 'oaset-vision-click', 0) == 1 else 1)",
    ])
    try:
        import ctypes

        if not ctypes.windll.user32.GetForegroundWindow():
            pytest.skip("no interactive foreground window (desktop locked/detached)")
        window = _wait_window(backend, "oaset-vision-click")
        assert window is not None, "MessageBox never appeared"

        # vision screenshot of the real window (gated, audited)
        shot = backend.capture(window.handle)
        assert shot.data[:2] == b"BM"
        assert shot.metadata()["coordinate_space"] == "virtual_screen_pixels"

        # locate the OK button via the semantic layer to get a trustworthy
        # coordinate, then click it through the VISION path
        elements = backend.elements(window.handle)
        button = next((e for e in elements if e.control_type == "Button"), None)
        assert button is not None, f"button not found in {elements}"
        rect = button.rect
        cx = rect["x"] + rect["w"] // 2
        cy = rect["y"] + rect["h"] // 2

        import asyncio

        try:
            result = asyncio.run(vision.click_at(
                cx, cy, scale_factor=rect["scale_factor"],
                display_id=rect["display_id"], window_handle=window.handle))
        except DesktopError as exc:
            if "SetCursorPos" in str(exc):
                pytest.skip(f"input synthesis denied by session state: {exc}")
            raise
        assert result is True

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.1)
        assert child.poll() == 0, f"vision click did not close the box (rc={child.poll()})"
    finally:
        if child.poll() is None:
            child.kill()
        backend.close()


def test_real_vision_enter_closes_message_box():
    if not _interactive_desktop_available():
        pytest.skip("interactive desktop unavailable (workstation locked)")
    backend = UiaComBackend()
    vision = VisionFallbackPage(backend, AllowGate())
    child = subprocess.Popen([
        sys.executable, "-c",
        "import ctypes,sys; sys.exit(0 if ctypes.windll.user32.MessageBoxW("
        "0, 'body', 'oaset-vision-type', 0) == 1 else 1)",
    ])
    try:
        window = _wait_window(backend, "oaset-vision-type")
        assert window is not None
        import asyncio

        async def run():
            await vision.screenshot(window.handle)  # gated observation
            # Windows foreground lock: a background process cannot steal
            # focus, so attach our thread to the box's input queue first
            import ctypes

            hwnd = int(window.handle)
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            this_tid = kernel32.GetCurrentThreadId()
            box_tid = user32.GetWindowThreadProcessId(hwnd, None)
            user32.AttachThreadInput(this_tid, box_tid, True)
            user32.SetForegroundWindow(hwnd)
            user32.AttachThreadInput(this_tid, box_tid, False)
            if user32.GetForegroundWindow() != hwnd:
                pytest.skip("environment does not grant foreground focus "
                            "(headless/session-0 style desktop)")
            await vision.type_text("\r")  # Enter = OK on a MessageBox

        asyncio.run(run())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.1)
        assert child.poll() == 0, f"vision Enter did not close the box (rc={child.poll()})"
    finally:
        if child.poll() is None:
            child.kill()
        backend.close()


def test_real_vision_denial_keeps_box_open():
    if not _interactive_desktop_available():
        pytest.skip("interactive desktop unavailable (workstation locked)")
    backend = UiaComBackend()
    vision = VisionFallbackPage(backend, DenyGate())
    child = subprocess.Popen([
        sys.executable, "-c",
        "import ctypes; ctypes.windll.user32.MessageBoxW("
        "0, 'body', 'oaset-vision-deny', 0)",
    ])
    try:
        window = _wait_window(backend, "oaset-vision-deny")
        assert window is not None
        import asyncio

        for coro in (
            vision.screenshot(window.handle),
            vision.click_at(10, 10),
            vision.type_text("x"),
        ):
            with pytest.raises(DesktopError) as excinfo:
                asyncio.run(coro)
            assert excinfo.value.code == "permission_denied"
        assert child.poll() is None, "denied run must leave the box open"
    finally:
        if child.poll() is None:
            child.kill()
        backend.close()
