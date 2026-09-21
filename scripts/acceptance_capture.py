"""Real-machine acceptance capture for TUI-01r / DESK-02r.

This does NOT replace human judgement, but it removes the mechanical part of
it: it launches the real TUI in a REAL console window at each matrix size,
captures a PNG of the actual window, records machine facts (monitors, DPI,
UIA window list) and runs the real UIA/vision suites, then writes an evidence
bundle with a manifest.

What stays human-only is stated in the report: a genuine IME composition
(Enter committing a Chinese candidate) and multi-monitor/UAC switching cannot
be synthesised by this script and must be performed and signed off by a person.

Usage:  python scripts/acceptance_capture.py [--out DIR] [--sizes 80x24,...]
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import subprocess
import sys

if sys.platform == "win32":
    import ctypes.wintypes as wt
else:  # pragma: no cover - Windows-only harness, importable everywhere
    wt = None  # type: ignore[assignment]
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance_probes as probes  # noqa: E402

DEFAULT_SIZES = ("80x24", "100x30", "120x40", "160x50")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

user32.SetProcessDPIAware()
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2 when available
except Exception:
    pass



def _rel(path: Path) -> str:
    """Path relative to the repo when possible, absolute otherwise."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())

# --------------------------------------------------------------------- PNG out

def bmp_to_png(bmp: bytes, out_path: Path) -> tuple[int, int]:
    """Convert the 32-bit BMP our GDI capture produces into a PNG (zlib only)."""
    if bmp[:2] != b"BM":
        raise ValueError("not a BMP payload")
    data_offset = struct.unpack_from("<I", bmp, 10)[0]
    header_size = struct.unpack_from("<I", bmp, 14)[0]
    width, height = struct.unpack_from("<ii", bmp, 18)
    bit_count = struct.unpack_from("<H", bmp, 28)[0]
    if header_size < 40 or bit_count not in (24, 32):
        raise ValueError(f"unsupported BMP: header={header_size} bits={bit_count}")
    top_down = height < 0
    height = abs(height)
    bpp = bit_count // 8
    stride = ((width * bit_count + 31) // 32) * 4
    raw = bytearray()
    for y in range(height):
        src_row = y if top_down else height - 1 - y
        start = data_offset + src_row * stride
        row = bmp[start:start + stride]
        line = bytearray([0])  # PNG filter type 0
        for x in range(width):
            b, g, r = row[x * bpp], row[x * bpp + 1], row[x * bpp + 2]
            line += bytes((r, g, b))
        raw += line

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    out_path.write_bytes(png)
    return width, height


def grab_window(hwnd: int, out_png: Path, out_bmp: Path) -> tuple[int, int]:
    """BitBlt the window into a BMP, then persist both BMP and PNG."""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise RuntimeError(f"invalid window rect for hwnd {hwnd}")

    hdc_window = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
    bitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
    old = gdi32.SelectObject(hdc_mem, bitmap)
    SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
    gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_window, 0, 0, SRCCOPY | CAPTUREBLT)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_ulong), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
                    ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_ulong),
                    ("biSizeImage", ctypes.c_ulong), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_ulong),
                    ("biClrImportant", ctypes.c_ulong)]

    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth, header.biHeight = width, -height  # top-down
    header.biPlanes, header.biBitCount = 1, 32
    header.biCompression = 0  # BI_RGB
    stride = width * 4
    buffer = ctypes.create_string_buffer(stride * height)
    gdi32.GetDIBits(hdc_mem, bitmap, 0, height, buffer, ctypes.byref(header), 0)

    bmp = bytearray()
    pixel_offset = 14 + ctypes.sizeof(BITMAPINFOHEADER)
    bmp += b"BM" + struct.pack("<IHHI", pixel_offset + len(buffer), 0, 0, pixel_offset)
    bmp += bytes(header)
    bmp += buffer.raw
    out_bmp.write_bytes(bytes(bmp))

    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_window)
    return bmp_to_png(bytes(bmp), out_png)


def find_window_for_pid(pid: int) -> int:
    found: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def callback(hwnd, _lparam):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found[0] if found else 0


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


# --------------------------------------------------- real console text buffer

def read_console_text(pid: int) -> dict:
    """Attach to the child console and read its character grid.

    Pixels need eyes; the console buffer does not: the actual rendered rows let
    this script assert geometry, layout and input response on the REAL terminal
    (and give a human a text record to review alongside the PNG).
    """
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
    OPEN_EXISTING = 3
    out: dict = {}
    if not kernel32.FreeConsole():
        pass
    if not kernel32.AttachConsole(pid):
        return {"error": f"AttachConsole failed (err={kernel32.GetLastError()})"}
    try:
        handle = kernel32.CreateFileW("CONOUT$", GENERIC_READ | GENERIC_WRITE,
                                      FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                      OPEN_EXISTING, 0, None)
        if handle in (None, 0, -1):
            return {"error": "could not open CONOUT$"}

        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                        ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

        class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
            _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                        ("wAttributes", ctypes.c_ushort), ("srWindow", SMALL_RECT),
                        ("dwMaximumWindowSize", COORD)]

        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return {"error": "GetConsoleScreenBufferInfo failed"}
        cols = info.srWindow.Right - info.srWindow.Left + 1
        rows = info.srWindow.Bottom - info.srWindow.Top + 1
        written = ctypes.c_ulong(0)
        lines: list[str] = []
        for row in range(rows):
            buffer = ctypes.create_unicode_buffer(cols + 1)
            kernel32.ReadConsoleOutputCharacterW(
                handle, buffer, cols, COORD(0, row), ctypes.byref(written))
            lines.append(buffer.value.rstrip())
        out = {"cols": cols, "rows": rows, "lines": lines}
        kernel32.CloseHandle(handle)
    finally:
        kernel32.FreeConsole()
    return out


def check_frame(frame: dict, size: str) -> list[str]:
    """Assertions a human would otherwise have to eyeball, in text form."""
    problems: list[str] = []
    if frame.get("error"):
        return [frame["error"]]
    cols, rows = (int(part) for part in size.split("x"))
    if frame["cols"] != cols:
        problems.append(f"console width is {frame['cols']}, expected {cols}")
    if frame["rows"] > rows:
        problems.append(f"console height {frame['rows']} exceeds the requested {rows}")
    non_empty = [ln for ln in frame["lines"] if ln.strip()]
    if not non_empty:
        problems.append("console buffer is empty — nothing was painted")
    if len(non_empty) < 3:
        problems.append(f"only {len(non_empty)} non-empty rows — layout looks collapsed")
    # the input row must exist separately from the status row (no overlap)
    bottom = frame["lines"][-1].strip()
    if not bottom:
        problems.append("last row (status bar) is empty")
    return problems


# ------------------------------------------------------------------- launching

def _title_for(size: str) -> str:
    return f"oaset-acceptance-{size}"


def _terminal_command(cols: int, rows: int, size: str) -> list[str]:
    """A real console at a fixed cell size, running the installed TUI.

    No `start` indirection: the process we spawn OWNS the new console, so its
    pid is the one AttachConsole needs for reading the real character grid.
    """
    # relative, space-free path: cmd needs no quoting gymnastics that way
    python = Path(".venv") / "Scripts" / "python.exe"
    launcher = f"{python} -m oaset --mock" if (ROOT / python).is_file() else "oaset --mock"
    inner = (f"mode con: cols={cols} lines={rows} "
             f"& title {_title_for(size)} & {launcher}")
    return ["cmd", "/k", inner]


def launch_terminal(cols: int, rows: int, size: str) -> tuple[subprocess.Popen, int]:
    proc = subprocess.Popen(_terminal_command(cols, rows, size), cwd=str(ROOT),
                            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    deadline = time.monotonic() + 20
    title_hwnd = 0
    wanted = _title_for(size)
    while time.monotonic() < deadline:
        for hwnd in _all_windows():
            if window_title(hwnd).startswith(wanted):
                title_hwnd = hwnd
                break
        if title_hwnd:
            break
        time.sleep(0.3)
    if not title_hwnd:
        proc.terminate()
        raise RuntimeError("oaset console window never appeared")
    return proc, title_hwnd


def _all_windows() -> list[int]:
    out: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            out.append(int(hwnd))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return out


def inject_console_keys(pid: int, text: str) -> dict:
    """Type into the child console WITHOUT needing window focus.

    SendInput depends on the foreground window, which a background session
    cannot reliably own; WriteConsoleInput puts key records straight into the
    console input buffer the app reads.
    """
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
    OPEN_EXISTING = 3
    KEY_EVENT = 0x0001

    class KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [("bKeyDown", wt.BOOL), ("wRepeatCount", ctypes.c_ushort),
                    ("wVirtualKeyCode", ctypes.c_ushort),
                    ("wVirtualScanCode", ctypes.c_ushort),
                    ("UnicodeChar", ctypes.c_wchar),
                    ("dwControlKeyState", ctypes.c_ulong)]

    class INPUT_RECORD(ctypes.Structure):
        class _EVENT(ctypes.Union):
            _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]

        _anonymous_ = ("Event",)
        _fields_ = [("EventType", ctypes.c_ushort), ("Event", _EVENT)]

    python = Path(".venv") / "Scripts" / "python.exe"
    launcher = f"{python} -m oaset --mock" if (ROOT / python).is_file() else "oaset --mock"
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        return {"error": f"AttachConsole failed (err={kernel32.GetLastError()})"}
    try:
        handle = kernel32.CreateFileW("CONIN$", GENERIC_READ | GENERIC_WRITE,
                                      FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                      OPEN_EXISTING, 0, None)
        if handle in (None, 0, -1):
            return {"error": "could not open CONIN$"}
        def send(chars: list[tuple[int, str]]) -> int:
            records = []
            for vk, ch in chars:
                for down in (True, False):
                    event = INPUT_RECORD()
                    event.EventType = KEY_EVENT
                    event.KeyEvent = KEY_EVENT_RECORD(down, 1, vk, 0, ch, 0)
                    records.append(event)
            array = (INPUT_RECORD * len(records))(*records)
            written = ctypes.c_ulong(0)
            kernel32.WriteConsoleInputW(handle, array, len(records),
                                        ctypes.byref(written))
            return int(written.value)

        text_records = send([(0, ch) for ch in text])
        time.sleep(0.6)
        enter_records = send([(0x0D, chr(13))])  # VK_RETURN in its own batch
        time.sleep(0.6)
        enter_records += send([(0x0D, chr(13))])  # the submit-settle window
        kernel32.CloseHandle(handle)
        return {"ok": True, "text_records": text_records,
                "enter_records": enter_records, "launcher": launcher}
    finally:
        kernel32.FreeConsole()


def _send_keys(hwnd: int, text: str) -> bool:
    """Type text into the focused console (best effort; focus may be refused)."""
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    if user32.GetForegroundWindow() != hwnd:
        return False
    KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("ki", KEYBDINPUT),
                    ("padding", ctypes.c_ubyte * 8)]

    events = []
    for ch in text:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            event = INPUT(type=1)
            event.ki = KEYBDINPUT(0, ord(ch), flags, 0, None)
            events.append(event)
    for vk in (0x0D,):  # Enter
        for flags in (0, KEYEVENTF_KEYUP):
            event = INPUT(type=1)
            event.ki = KEYBDINPUT(vk, 0, flags, 0, None)
            events.append(event)
    array = (INPUT * len(events))(*events)
    user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    return True


# --------------------------------------------------------------- desktop facts

def display_facts() -> dict:
    monitors: list[dict] = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(wt.RECT), ctypes.c_void_p)

    def callback(_hmon, _hdc, lprc, _data):
        rect = lprc.contents
        monitors.append({"left": rect.left, "top": rect.top,
                         "width": rect.right - rect.left,
                         "height": rect.bottom - rect.top})
        return 1

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), None)
    dpi = 0
    try:
        dpi = int(user32.GetDpiForSystem())
    except Exception:
        pass
    return {
        "monitors": monitors,
        "virtual_screen": [user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)],
        "system_dpi": dpi,
        "scale_factor": round(dpi / 96, 2) if dpi else None,
        "primary_scale_note": "collected from the real desktop",
    }


def uia_window_sample(limit: int = 8) -> list[dict]:
    try:
        from oaset.computer.uia_backend import UiaComBackend
    except Exception as exc:  # pragma: no cover - non-Windows
        return [{"error": f"{type(exc).__name__}: {exc}"}]
    try:
        backend = UiaComBackend()
        rows = []
        for window in backend.list_windows()[:limit]:
            rows.append({"title": window.title[:60], "pid": window.pid,
                         "rect": window.rect})
        return rows
    except Exception as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"}]


def run_real_suites(out_dir: Path) -> dict:
    """Run the real UIA/vision suites and archive their output as evidence."""
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    cmd = [str(python), "-m", "pytest", "tests/test_uia_real.py",
           "tests/test_vision_real.py", "-q", "-rs", "-p", "no:cacheprovider"]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    output = (proc.stdout or "") + (proc.stderr or "")
    (out_dir / "real-suites.log").write_text(output, encoding="utf-8")
    tail = [line for line in output.splitlines() if line.strip()][-3:]
    return {"exit_code": proc.returncode, "summary": tail,
            "log": _rel(out_dir / "real-suites.log")}


# ------------------------------------------------------------------- main flow


def terminal_hosts() -> dict:
    """Which real terminal hosts exist on this machine (evidence, not prose)."""
    import shutil as _shutil

    wt = _shutil.which("wt") or ""
    wt_alias = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "wt.exe"
    return {
        "conhost": {"available": True, "path": str(Path(os.environ.get("SystemRoot", "C:/Windows"))
                                                     / "System32" / "conhost.exe")},
        "windows_terminal": {"available": bool(wt or wt_alias.is_file()),
                             "path": wt or (str(wt_alias) if wt_alias.is_file() else "")},
        "note": ("matrix rows are only capturable for hosts present on this machine; "
                 "an unavailable host must be tested where it exists"),
    }

def capture_matrix(out_dir: Path, sizes: tuple[str, ...]) -> list[dict]:
    """Capture each matrix size in order: render → /help → resize sweep.

    Order matters: the help frame is captured from a pristine window first; the
    resize sweep then proves live re-layout. Clipboard chords are recorded as an
    attempt plus the documented limitation (console input records do not become
    Textual modifier bindings), never as a pass.
    """
    results: list[dict] = []
    for size in sizes:
        cols, rows = (int(part) for part in size.split("x"))
        entry: dict = {"terminal": "conhost (CREATE_NEW_CONSOLE)", "size": size}
        proc = None
        try:
            proc, hwnd = launch_terminal(cols, rows, size)
            entry["hwnd"] = hwnd
            owner = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            entry["title"] = window_title(hwnd)
            entry["window_owner_pid"] = int(owner.value)
            entry["window_matches_process"] = int(owner.value) == proc.pid
            if not entry["window_matches_process"]:
                entry.setdefault("problems", []).append(
                    f"captured window belongs to pid {owner.value}, "
                    f"not the launched {proc.pid}")
            time.sleep(4.0)  # let Textual paint the first frame

            png = out_dir / f"tui-{size}-initial.png"
            image_w, image_h = grab_window(hwnd, png, out_dir / f"tui-{size}-initial.bmp")
            entry["initial_png"] = _rel(png)
            entry["window_pixels"] = [image_w, image_h]
            (out_dir / f"tui-{size}-initial.bmp").unlink(missing_ok=True)

            frame = read_console_text(proc.pid)
            requested = (cols, rows)
            entry["console"] = {"cols": frame.get("cols"), "rows": frame.get("rows"),
                                "requested": list(requested),
                                "rows_clamped_by_host":
                                    bool(frame.get("rows") and frame["rows"] < rows)}
            entry["problems"] = check_frame(frame, size)
            if frame.get("lines"):
                text_path = out_dir / f"tui-{size}-initial.txt"
                text_path.write_text(chr(10).join(frame["lines"]), encoding="utf-8")
                entry["initial_txt"] = _rel(text_path)

            # 1) drive /help in the real console (no focus needed)
            injection = inject_console_keys(proc.pid, "/help")
            entry["input_injection"] = injection
            typed = bool(injection.get("ok"))
            entry["input_injected"] = typed
            if typed:
                time.sleep(2.5)
                png_help = out_dir / f"tui-{size}-help.png"
                grab_window(hwnd, png_help, out_dir / f"tui-{size}-help.bmp")
                (out_dir / f"tui-{size}-help.bmp").unlink(missing_ok=True)
                entry["help_png"] = _rel(png_help)
                after = read_console_text(proc.pid)
                if after.get("lines"):
                    text_path = out_dir / f"tui-{size}-help.txt"
                    text_path.write_text(chr(10).join(after["lines"]), encoding="utf-8")
                    entry["help_txt"] = _rel(text_path)
                    joined = " ".join(after["lines"]).lower()
                    slash_rows = sum(1 for ln in after["lines"]
                                     if ln.strip().startswith(("✎", "⚠", "/")))
                    entry["help_responded"] = (
                        "ctrl+k" in joined or "快捷键" in joined
                        or "shortcut" in joined or slash_rows >= 3)
                    entry["help_command_rows"] = slash_rows

            # 2) live resize: every achieved grid must re-lay out cleanly
            try:
                calibration = probes.calibrate_cell(hwnd, proc.pid, read_console_text)
                cell_w, cell_h = (calibration.get("cell") or [8, 16])
                chrome = calibration.get("chrome") or [16, 39]
                sweep = []
                for target_cols, target_rows in ((100, 30), (80, 24), (120, 40), (80, 24)):
                    resize = probes.resize_window(hwnd, target_cols, target_rows,
                                                  cell_w, cell_h, chrome)
                    time.sleep(1.8)
                    grid = read_console_text(proc.pid)
                    settle = probes.settle_check(read_console_text, proc.pid)
                    sweep.append({
                        "requested": [target_cols, target_rows],
                        "grid": [grid.get("cols"), grid.get("rows")],
                        "reached_request": grid.get("cols") == target_cols,
                        "problems": check_frame(grid, f"{grid.get('cols')}x{grid.get('rows')}"),
                        "settle": settle,
                        "resize": resize,
                    })
                entry["cell_calibration"] = calibration
                entry["resize_sweep"] = sweep
                entry["resize_sweep_clean"] = all(not s["problems"] for s in sweep)
                entry["resize_sweep_reached_all"] = all(s["reached_request"] for s in sweep)
                entry["resize_sweep_settled"] = all(
                    (s.get("settle") or {}).get("settled") for s in sweep)
            except Exception as exc:
                entry["probe_error"] = f"{type(exc).__name__}: {exc}"

            # 3) clipboard chord: attempt + documented limitation (not a pass)
            try:
                entry["clipboard_paste_attempt"] = probes.clipboard_paste_probe(
                    proc.pid, read_console_text, f"oaset-paste-{size}")
            except Exception as exc:
                entry["clipboard_paste_attempt"] = {"error": f"{type(exc).__name__}: {exc}"}
            entry["clipboard_paste_note"] = (
                "console input records do not carry modifier chords into Textual "
                "bindings; human verification required")

            # 4) IME-like burst (one-send guarantee) and real mouse click
            try:
                entry["ime_burst"] = probes.ime_burst_probe(
                    proc.pid, read_console_text, "中文测试甲")
            except Exception as exc:
                entry["ime_burst"] = {"error": f"{type(exc).__name__}: {exc}"}
            try:
                entry["mouse_click"] = probes.mouse_click_probe(
                    proc.pid, read_console_text)
            except Exception as exc:
                entry["mouse_click"] = {"error": f"{type(exc).__name__}: {exc}"}
            try:
                entry["mouse_vt_click"] = probes.mouse_vt_click_probe(
                    proc.pid, read_console_text)
            except Exception as exc:
                entry["mouse_vt_click"] = {"error": f"{type(exc).__name__}: {exc}"}
            try:
                entry["ime_commit"] = probes.ime_commit_probe(
                    hwnd, proc.pid, read_console_text, "你好世界")
            except Exception as exc:
                entry["ime_commit"] = {"error": f"{type(exc).__name__}: {exc}"}
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if proc is not None:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True)
        results.append(entry)
    return results




def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="real-machine acceptance capture")
    parser.add_argument("--out", default="", help="output dir (default docs/evidence/real-machine/<ts>)")
    parser.add_argument("--sizes", default=",".join(DEFAULT_SIZES))
    parser.add_argument("--merge-attestation", default="",
                        help="path to a filled attestation.json; writes verdicts "
                             "into docs/evidence/manual-matrix.md (refuses when incomplete)")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --merge-attestation: report without writing")
    parser.add_argument("--attest", default="",
                        help="path to an evidence dir: prompt for the five human "
                             "verdicts and write attestation.json")
    args = parser.parse_args(argv)

    if args.attest:
        result = probes.attest_interactive(Path(args.attest).resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2

    if args.merge_attestation:
        result = probes.merge_attestation(Path(args.merge_attestation),
                                   ROOT / "docs" / "evidence" / "manual-matrix.md",
                                   dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = (Path(args.out) if args.out
               else ROOT / "docs" / "evidence" / "real-machine" / stamp).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    sizes = tuple(s.strip() for s in args.sizes.split(",") if s.strip())

    report: dict = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "machine": {"platform": sys.platform, "python": sys.version.split()[0]},
        "terminal_hosts": terminal_hosts(),
        "display": display_facts(),
        "uia_windows_sample": uia_window_sample(),
        "matrix": capture_matrix(out_dir, sizes),
        "real_suites": run_real_suites(out_dir),
        "human_only": [
            "TUI-02r real IME: typing Chinese through Microsoft Pinyin and letting "
            "Enter commit the candidate — SendInput cannot synthesise an IME "
            "composition, so this must be performed by a person.",
            "DESK-02r multi-monitor / UAC: this machine has "
            f"{len(display_facts()['monitors'])} monitor(s); multi-screen moves and "
            "UAC elevation prompts need a human on the target hardware.",
        ],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# Real-machine acceptance capture", "",
             f"- captured_at: {report['captured_at']}",
             f"- displays: {json.dumps(report['display'], ensure_ascii=False)}",
             f"- real UIA/vision suites: exit={report['real_suites']['exit_code']} "
             f"{report['real_suites']['summary']}", "",
             "| terminal | size | grid | window px | problems | /help | "
             "resize sweep (all sizes reached, no layout problems) |",
             "|---|---|---|---|---|---|---|"]
    for row in report["matrix"]:
        grid = row.get("console") or {}
        lines.append(f"| {row['terminal']} | {row['size']} | "
                     f"{grid.get('cols', '-')}x{grid.get('rows', '-')} | "
                     f"{row.get('window_pixels', '-')} | "
                     f"{'; '.join(row.get('problems') or []) or 'none'} | "
                     f"{row.get('help_responded', '-')} | "
                     f"{row.get('resize_sweep_clean', '-')} / "
                     f"{row.get('resize_sweep_reached_all', '-')} |")
    lines.append("")
    lines.append("Text records: `tui-<size>-initial.txt`, `tui-<size>-help.txt` "
                 "(the real console character grid, reviewable without a display).")
    lines += ["", "## Still requires a human", ""]
    lines += [f"- {item}" for item in report["human_only"]]
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"evidence bundle: {out_dir}")
    for row in report["matrix"]:
        print(f"  {row['size']}: {row.get('initial_png') or row.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
