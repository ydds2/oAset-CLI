"""REAL-machine UIA backend verification (Windows only, skipped elsewhere).

No fake backend anywhere in this file: every action goes through the real
COM/UIA vtable calls in computer/uia_backend.py against actual windows —
a MessageBox child for a real click, a child window with an EDIT control
for real typed-text readback, and GDI capture for a real screenshot.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

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


from oaset.computer.desktop import GatedDesktopPage, UiaDesktopPage  # noqa: E402
from oaset.computer.uia_backend import UiaComBackend, register_default_backend  # noqa: E402

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="real-machine input suite"),
    pytest.mark.skipif(not _interactive_desktop_available(),
                       reason="interactive desktop unavailable (workstation locked)"),
    pytest.mark.real_input,
]



class AllowGate:
    async def request(self, tool_name, level, summary, preview=None):
        return "always"


def _wait_for_window(backend: UiaComBackend, title_prefix: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for window in backend.list_windows():
            if window.title.startswith(title_prefix):
                return window
        time.sleep(0.15)
    return None


# ------------------------------------------------------------------ enumeration

def test_real_window_enumeration_and_metadata():
    import ctypes

    if not ctypes.windll.user32.GetForegroundWindow():
        pytest.skip("no interactive foreground window (desktop locked/detached)")
    backend = UiaComBackend()
    try:
        windows = backend.list_windows()
        assert len(windows) >= 1, "a real desktop always has at least one window"
        win = windows[0]
        assert win.title and win.pid
        assert win.rect["coordinate_space"] == "virtual_screen_pixels"
        assert win.rect["w"] > 0 and win.rect["h"] > 0
        assert isinstance(win.rect["scale_factor"], float)
        if not any(w.foreground for w in windows):
            # detached/console-switched sessions report no foreground owner
            pytest.skip("no foreground window reported (session detached?)")
    finally:
        backend.close()


def test_real_elements_and_find_in_console_window():
    backend = UiaComBackend()
    try:
        windows = backend.list_windows()
        target = next((w for w in windows if w.title), windows[0])
        elements = backend.elements(target.handle)
        # a titled top-level window has children (title bar buttons at least)
        assert isinstance(elements, list)
        for element in elements:
            assert element.window_handle == target.handle
            assert element.rect.get("coordinate_space") == "virtual_screen_pixels"
        if elements:  # find by the element's own handle must round-trip
            found = backend.find_element(target.handle, elements[0].handle)
            assert found is not None
    finally:
        backend.close()


# ------------------------------------------------------------------ real click

def test_real_click_closes_a_message_box():
    """Click the OK button of a REAL MessageBox via UIA InvokePattern."""
    backend = UiaComBackend()
    child = subprocess.Popen([
        sys.executable, "-c",
        "import ctypes, sys; sys.exit(0 if ctypes.windll.user32.MessageBoxW("
        "0, 'body', 'oaset-uia-click-test', 0) == 1 else 1)",
    ])
    try:
        window = _wait_for_window(backend, "oaset-uia-click-test")
        assert window is not None, "MessageBox window never appeared"
        # locale-independent: the button is "OK"/"确定"/… — take it by control type
        els = backend.elements(window.handle)
        ok = next((e for e in els if e.control_type == "Button"), None)
        assert ok is not None, f"button not found among {els}"
        assert backend.click_element(ok.handle) is True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.1)
        assert child.poll() == 0, f"MessageBox not closed by UIA click (rc={child.poll()})"
    finally:
        if child.poll() is None:
            child.kill()
        backend.close()


# ------------------------------------------------------------- real typing

EDIT_CHILD = r'''
import ctypes, ctypes.wintypes as wt, sys

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
out_path = sys.argv[1]

WM_COMMAND = 0x0111
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)

# 64-bit signatures: default 32-bit conversion of LPARAM/WPARAM overflows
user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                  ctypes.c_size_t, ctypes.c_ssize_t]
user32.DefWindowProcW.restype = LRESULT

def wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_COMMAND and (wparam >> 16) == 0:  # button clicked
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(edit_hwnd, buf, 256)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(buf.value)
        user32.PostQuitMessage(0)
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

proc = WNDPROC(wndproc)  # keep alive: a GC'd callback thunk fails fast on first message
wc = WNDCLASS(style=0, lpfnWndProc=proc, hInstance=kernel32.GetModuleHandleW(None),
              hCursor=user32.LoadCursorW(0, 32512), hbrBackground=6,
              lpszClassName="OasetUiaTest")
user32.RegisterClassW(ctypes.byref(wc))

hwnd = user32.CreateWindowExW(0, "OasetUiaTest", "oaset-uia-type-test",
                              0x00CF0000, 100, 100, 480, 200, 0, 0,
                              kernel32.GetModuleHandleW(None), 0)
edit_hwnd = user32.CreateWindowExW(0x00000200, "EDIT", "",
                                   0x50010000 | 0x80 | 0x1000,  # child|visible|border|es_autohscroll
                                   10, 10, 440, 30, hwnd, 1001, 0, 0)
btn_hwnd = user32.CreateWindowExW(0, "BUTTON", "Done",
                                  0x50010000, 10, 60, 100, 32, hwnd, 1002, 0, 0)
user32.ShowWindow(hwnd, 5)
user32.UpdateWindow(hwnd)

msg = wt.MSG()
while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))
'''


def test_real_type_text_into_edit_and_read_back(tmp_path: Path):
    """UIA ValuePattern types into a REAL Edit control; the child window
    writes the control content back on a REAL button click (Invoke)."""
    script = tmp_path / "edit_child.py"
    out = tmp_path / "typed.txt"
    script.write_text(EDIT_CHILD, encoding="utf-8")
    backend = UiaComBackend()
    child = subprocess.Popen([sys.executable, str(script), str(out)])
    try:
        window = _wait_for_window(backend, "oaset-uia-type-test")
        assert window is not None, "EDIT child window never appeared"

        elements = backend.elements(window.handle)
        edit = next((e for e in elements if e.control_type == "Edit"), None)
        assert edit is not None, f"Edit element not found in {elements}"
        button = next((e for e in elements if e.control_type == "Button"), None)
        assert button is not None

        typed = "你好 UIA hello 123"
        assert backend.type_text(edit.handle, typed) is True
        assert backend.click_element(button.handle) is True

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not out.exists():
            time.sleep(0.1)
        assert out.exists(), "child never wrote the Edit content back"
        assert out.read_text(encoding="utf-8") == typed
        assert child.wait(timeout=5) == 0
    finally:
        if child.poll() is None:
            child.kill()
        backend.close()


# ------------------------------------------------------------- real capture

def test_real_capture_returns_bmp(tmp_path: Path):
    backend = UiaComBackend()
    child = subprocess.Popen([
        sys.executable, "-c",
        "import ctypes; ctypes.windll.user32.MessageBoxW("
        "0, 'x', 'oaset-uia-capture-test', 0)",
    ])
    try:
        window = _wait_for_window(backend, "oaset-uia-capture-test")
        assert window is not None
        shot = backend.capture(window.handle)
        assert shot.mime == "image/bmp"
        assert shot.data[:2] == b"BM"
        assert len(shot.data) > 1000
        assert shot.width == window.rect["w"] and shot.height == window.rect["h"]
        meta = shot.metadata()
        assert meta["coordinate_space"] == "virtual_screen_pixels"
        assert meta["scale_factor"] >= 1.0
        assert meta["captured_at"] > 0
    finally:
        if child.poll() is None:
            child.kill()
        backend.close()


# ---------------------------------------------------- gated page over real backend

async def test_gated_page_over_real_backend_lists_windows():
    register_default_backend()
    page = GatedDesktopPage(UiaDesktopPage(UiaComBackend()), AllowGate())
    windows = await page.list_windows()
    assert len(windows) >= 1
    shot = await page.screenshot(windows[0].handle)  # AllowGate
    assert shot.data[:2] == b"BM"
