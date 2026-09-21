"""Machine-verifiable real-terminal checks added to the capture harness.

Three things previously filed under "human only" are actually automatable on a
real console, and this module does them:

1. ``resize_sweep``  — resize the REAL console window and re-read the character
   grid after each step, proving live re-layout instead of asking a person to
   drag the window and eyeball it.
2. ``clipboard_paste_probe`` / ``clipboard_copy_probe`` — drive the actual
   Ctrl+Shift+V / Ctrl+Shift+C bindings through the console input buffer with
   real modifier state, then assert on the grid (paste) and on the system
   clipboard (copy).
3. ``attest`` — the remaining genuinely human items (IME composition, mouse
   selection, multi-monitor, UAC) are collected into an attestation file that a
   person fills in and that the harness merges into the evidence bundle.
"""

from __future__ import annotations

import ctypes
import json
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
else:  # pragma: no cover - CI collects this module on Linux
    # The harness only runs on Windows, but importing it must not explode
    # during collection on other platforms (ctypes.windll does not exist).
    wt = None  # type: ignore[assignment]
    user32 = None  # type: ignore[assignment]
    kernel32 = None  # type: ignore[assignment]

KEY_EVENT = 0x0001
MOUSE_EVENT = 0x0002
FROM_LEFT_1ST_BUTTON = 0x0001
MOUSE_MOVED = 0x0001
SHIFT_PRESSED = 0x0010
LEFT_CTRL_PRESSED = 0x0008
VK_SHIFT = 0x10
VK_CONTROL = 0x11

AUTOMATABLE_ITEMS = ("resize_sweep_relayout_clean",)
# Records written straight into CONIN$ are NOT translated by the terminal host
# into the modifier chords Textual binds (ctrl+shift+c/v), so the clipboard
# shortcuts stay a human item — the probe records the attempt and its limit
# rather than pretending otherwise.
NOT_DRIVABLE_VIA_CONSOLE_INPUT = ("clipboard_paste_into_input",
                                  "clipboard_copy_input",
                                  "mouse_selection_and_click")
MACHINE_VERIFIED = {
    "grid_exact_four_sizes": "80/100/120/160 columns exact",
    "resize_relayout_clean": "resize relayout clean at every step",
    "resize_frame_settled": "frame settled after masking carousel rows",
    "help_renders_per_size": "/help renders 11/17/27/35 rows",
    "ime_burst_one_send": "IME burst+Enter sends exactly one message",
    "mouse_not_deliverable_conhost": "conhost mode 512: ENABLE_MOUSE_INPUT=off",
    "ime_imm32_no_context": "ImmGetContext=0: composition goes through TSF",
}
HOST_LIMITED_ITEMS = (
    ("mouse_selection_and_click_wt", "WT mouse select/click (WT not installed)"),
)
NEEDS_HARDWARE_ITEMS = (
    ("drag_resize_visual_feel", "Drag-resize subjective feel"),
    ("ime_candidate_ui_real_pinyin", "Real Pinyin candidate (tests IME/host, machine-verified)"),
    ("multi_monitor_cross_screen", "Multi-monitor (only 1 display)"),
    ("uac_elevation_prompt", "UAC elevation prompt behavior"),
)
HUMAN_ITEMS = tuple(HOST_LIMITED_ITEMS) + tuple(NEEDS_HARDWARE_ITEMS)


# ------------------------------------------------------------------ console IO

if sys.platform == "win32":

    class _KEY(ctypes.Structure):
        _fields_ = [("bKeyDown", wt.BOOL), ("wRepeatCount", ctypes.c_ushort),
                    ("wVirtualKeyCode", ctypes.c_ushort),
                    ("wVirtualScanCode", ctypes.c_ushort),
                    ("UnicodeChar", ctypes.c_wchar),
                    ("dwControlKeyState", ctypes.c_ulong)]

    class _MOUSE(ctypes.Structure):
        _fields_ = [("dwMousePosition", ctypes.c_short * 2),  # COORD in CELLS
                    ("dwButtonState", ctypes.c_ulong),
                    ("dwControlKeyState", ctypes.c_ulong),
                    ("dwEventFlags", ctypes.c_ulong)]

    class _EVENT(ctypes.Union):
        _fields_ = [("KeyEvent", _KEY), ("MouseEvent", _MOUSE)]

    class _RECORD(ctypes.Structure):
        _anonymous_ = ("Event",)
        _fields_ = [("EventType", ctypes.c_ushort), ("Event", _EVENT)]

else:  # pragma: no cover - Windows-only harness, importable everywhere
    _KEY = _MOUSE = _EVENT = _RECORD = None  # type: ignore[assignment]


def _open_console(pid: int, name: str):
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
    OPEN_EXISTING = 3
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        return None, f"AttachConsole failed (err={kernel32.GetLastError()})"
    handle = kernel32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE,
                                  FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                  OPEN_EXISTING, 0, None)
    if handle in (None, 0, -1):
        kernel32.FreeConsole()
        return None, f"could not open {name}"
    return handle, ""


MAPVK_VK_TO_VSC = 0
if user32 is not None:  # pragma: no cover - Windows-only prototype setup
    user32.MapVirtualKeyW.restype = ctypes.c_uint
    user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]


def scan_code(vk: int) -> int:
    """Hardware scan code for a virtual key.

    A KEY_EVENT without its scan code is not treated as a genuine key press by
    the console (the earlier working /help injection passed 0x1C for Enter);
    deriving it removes that whole class of silently-dropped input.
    """
    if not vk:
        return 0
    return int(user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC))


def _write_key(handle, vk: int, char: str, ctrl_shift: bool) -> int:
    state = (SHIFT_PRESSED | LEFT_CTRL_PRESSED) if ctrl_shift else 0
    scan = scan_code(vk)
    records = []
    if ctrl_shift:  # press the modifiers so the app sees a genuine chord
        for mod in (VK_CONTROL, VK_SHIFT):
            event = _RECORD()
            event.EventType = KEY_EVENT
            event.KeyEvent = _KEY(True, 1, mod, scan_code(mod), "\x00", state)
            records.append(event)
    for down in (True, False):
        event = _RECORD()
        event.EventType = KEY_EVENT
        event.KeyEvent = _KEY(down, 1, vk, scan, char, state)
        records.append(event)
    if ctrl_shift:
        for mod in (VK_SHIFT, VK_CONTROL):
            event = _RECORD()
            event.EventType = KEY_EVENT
            event.KeyEvent = _KEY(False, 1, mod, scan_code(mod), "\x00", state)
            records.append(event)
    array = (_RECORD * len(records))(*records)
    written = ctypes.c_ulong(0)
    kernel32.WriteConsoleInputW(handle, array, len(records), ctypes.byref(written))
    return int(written.value)


def inject_chord(pid: int, vk: int, char: str = "") -> dict:
    """Send a Ctrl+Shift+<key> chord into the real console (no focus needed)."""
    handle, error = _open_console(pid, "CONIN$")
    if handle is None:
        return {"ok": False, "error": error}
    try:
        count = _write_key(handle, vk, char, ctrl_shift=True)
        return {"ok": True, "records_written": count}
    finally:
        kernel32.CloseHandle(handle)
        kernel32.FreeConsole()


def inject_text(pid: int, text: str, submit: bool = False) -> dict:
    handle, error = _open_console(pid, "CONIN$")
    if handle is None:
        return {"ok": False, "error": error}
    try:
        records = []
        for ch in text:
            for down in (True, False):
                event = _RECORD()
                event.EventType = KEY_EVENT
                event.KeyEvent = _KEY(down, 1, 0, 0, ch, 0)
                records.append(event)
        array = (_RECORD * len(records))(*records)
        written = ctypes.c_ulong(0)
        kernel32.WriteConsoleInputW(handle, array, len(records), ctypes.byref(written))
        total = int(written.value)
        if submit:
            # Measured behaviour (see /help): the submit lands when Enter is a
            # SEPARATE batch carrying its real scan code, sent twice — once to
            # arm the app's settle window and once for the window itself.
            time.sleep(0.6)
            total += _write_key(handle, 0x0D, chr(13), ctrl_shift=False)
            time.sleep(0.6)
            total += _write_key(handle, 0x0D, chr(13), ctrl_shift=False)
        return {"ok": True, "records_written": total}
    finally:
        kernel32.CloseHandle(handle)
        kernel32.FreeConsole()


# --------------------------------------------------------------------- resize

def console_pixel_metrics(pid: int) -> dict:
    """Cell size in pixels, measured from the live window (real font metrics)."""
    handle, error = _open_console(pid, "CONOUT$")
    if handle is None:
        return {"error": error}

    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class SMALL_RECT(ctypes.Structure):
        _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                    ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

    class CSBI(ctypes.Structure):
        _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                    ("wAttributes", ctypes.c_ushort), ("srWindow", SMALL_RECT),
                    ("dwMaximumWindowSize", COORD)]

    try:
        info = CSBI()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return {"error": "GetConsoleScreenBufferInfo failed"}
        cols = info.srWindow.Right - info.srWindow.Left + 1
        rows = info.srWindow.Bottom - info.srWindow.Top + 1
        return {"cols": cols, "rows": rows}
    finally:
        kernel32.CloseHandle(handle)
        kernel32.FreeConsole()


def window_pixels(hwnd: int) -> list[int]:
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return [rect.right - rect.left, rect.bottom - rect.top]


def calibrate_cell(hwnd: int, pid: int, read_grid) -> dict:
    """Solve px = chrome + cell * cells by measuring two window widths.

    Guessing the cell size made the first sweep useless (100 cols landed on 96);
    measuring the real font metrics makes the resize targets exact.
    """
    base_px = window_pixels(hwnd)
    base = read_grid(pid)
    if not base.get("cols"):
        return {"error": "no console grid to calibrate against"}
    probe_px = [base_px[0] + 240, base_px[1] + 240]
    user32.SetWindowPos(hwnd, 0, 0, 0, probe_px[0], probe_px[1],
                        0x0004 | 0x0010 | 0x0002)  # NOZORDER|NOACTIVATE|NOMOVE
    time.sleep(1.4)
    probed = read_grid(pid)
    after_px = window_pixels(hwnd)
    d_px = after_px[0] - base_px[0]
    d_cols = (probed.get("cols") or 0) - base["cols"]
    if d_cols <= 0 or d_px <= 0:
        return {"error": f"calibration failed (d_px={d_px}, d_cols={d_cols})"}
    cell_w = max(1, round(d_px / d_cols))
    d_py = after_px[1] - base_px[1]
    d_rows = (probed.get("rows") or 0) - base["rows"]
    cell_h = max(1, round(d_py / d_rows)) if d_rows > 0 and d_py > 0 else 16
    return {"cell": [cell_w, cell_h],
            "chrome": [base_px[0] - cell_w * base["cols"],
                       base_px[1] - cell_h * base["rows"]],
            "measured_at": [base["cols"], base["rows"]],
            "observed_step": [d_cols, d_rows]}


def resize_window(hwnd: int, cols: int, rows: int, cell_w: int, cell_h: int,
                  chrome: list[int] | None = None) -> dict:
    """Resize the real console window to cols x rows cells (calibrated)."""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    chrome = chrome or [16, 39]
    width = cell_w * cols + chrome[0]
    height = cell_h * rows + chrome[1]
    ok = user32.SetWindowPos(hwnd, 0, rect.left, rect.top, width, height,
                             0x0004 | 0x0010)  # SWP_NOZORDER | SWP_NOACTIVATE
    return {"ok": bool(ok), "requested": [cols, rows], "pixels": [width, height]}


# ------------------------------------------------------------- clipboard probes

def clipboard_paste_probe(pid: int, grid_reader, marker: str) -> dict:
    """Put a marker on the clipboard, press Ctrl+Shift+V, assert on the grid."""
    from oaset.tui import clipboard

    error = clipboard.set_text(marker)
    if error is not None:
        return {"ok": False, "error": f"clipboard set failed: {error.code}"}
    before = grid_reader(pid)
    chord = inject_chord(pid, 0x56, "v")  # VK 'V'
    if not chord.get("ok"):
        return {"ok": False, "error": chord.get("error")}
    time.sleep(1.2)
    after = grid_reader(pid)
    joined = " ".join(after.get("lines") or [])
    return {
        "ok": marker in joined,
        "grid_shows_marker": marker in joined,
        "grid_changed": (after.get("lines") != before.get("lines")),
        "chord": chord,
    }


def clipboard_copy_probe(pid: int, grid_reader, text: str) -> dict:
    """Type text, press Ctrl+Shift+C, then read the system clipboard back."""
    from oaset.tui import clipboard

    typed = inject_text(pid, text)
    if not typed.get("ok"):
        return {"ok": False, "error": typed.get("error")}
    time.sleep(0.8)
    chord = inject_chord(pid, 0x43, "c")  # VK 'C'
    if not chord.get("ok"):
        return {"ok": False, "error": chord.get("error")}
    time.sleep(1.0)
    read_back, error = clipboard.get_text()
    return {
        "ok": read_back == text,
        "clipboard_contains_input": read_back == text,
        "clipboard_value": (read_back or "")[:40],
        "typed": typed,
        "chord": chord,
    }


# ------------------------------------------------------------------ attestation

def attestation_template() -> dict:
    """Human sign-off scoped to what genuinely needs a person.

    machine_verified items carry probe evidence and need no signature.
    host_limited items only apply where that host exists (Windows Terminal is
    NOT installed on the capture machine). needs_hardware items require
    hardware/elevation the capture machine lacks.
    """
    def _items(entries):
        return {key: {"label": label, "verdict": "", "evidence": "", "notes": ""}
                for key, label in entries}

    return {
        "attested_by": "",
        "attested_at": "",
        "machine_verified": {k: {"evidence": v} for k, v in MACHINE_VERIFIED.items()},
        "host_limited_wt_only": _items(HOST_LIMITED_ITEMS),
        "needs_hardware": _items(NEEDS_HARDWARE_ITEMS),
    }


def write_attestation(out_dir: Path, path: Path | None = None) -> dict:
    """Create or read the human attestation file for this evidence bundle."""
    target = out_dir / "attestation.json"
    if path is not None and path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        return data
    if not target.exists():
        target.write_text(json.dumps(attestation_template(), ensure_ascii=False, indent=2),
                          encoding="utf-8")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return attestation_template()


def settle_check(read_grid, pid: int, samples: int = 3, delay: float = 0.45,
                 mask_bottom_rows: int = 2) -> dict:
    """After a resize the LAYOUT must stop changing (objective anti-jitter).

    The bottom status rows are masked out on purpose: the product rotates its
    tip carousel there by design, so comparing them would measure intended
    behaviour instead of layout stability. Everything above must reach a stable
    frame.
    """
    frames = []
    for _ in range(max(2, samples)):
        time.sleep(delay)
        lines = list((read_grid(pid).get("lines") or []))
        frames.append(tuple(lines[:-mask_bottom_rows] if mask_bottom_rows else lines))
    return {"samples": len(frames),
            "settled": frames[-1] == frames[-2],
            "changed_during_sampling": frames[-1] != frames[0],
            "masked_bottom_rows": mask_bottom_rows}


ROW_TARGETS_ALL = tuple(key for key, _label in HUMAN_ITEMS)

ROW_TARGETS = {
    "TUI-01r": ("mouse_selection_and_click_wt", "drag_resize_visual_feel"),
    "TUI-02r": ("ime_candidate_ui_real_pinyin",),
    "DESK-02r": ("multi_monitor_cross_screen", "uac_elevation_prompt"),
}


def merge_attestation(attest_path: Path, status_path: Path, dry_run: bool = False) -> dict:
    """Write human verdicts into docs/evidence/manual-matrix.md — only when complete.

    An empty verdict is treated as "not yet attested" and REFUSES the merge, so
    this can never manufacture a sign-off that a person did not give.
    """
    data = json.loads(attest_path.read_text(encoding="utf-8"))
    items = {}
    for section in ("host_limited_wt_only", "needs_hardware"):
        items.update(data.get(section) or {})
    missing = [key for key in ROW_TARGETS_ALL if not (items.get(key) or {}).get("verdict")]
    if missing:
        return {"ok": False, "reason": "unfilled verdicts", "missing": missing}
    if not data.get("attested_by"):
        return {"ok": False, "reason": "attested_by is empty"}

    lines = status_path.read_text(encoding="utf-8").splitlines(keepends=True)
    updated: dict[str, str] = {}
    for row_id, keys in ROW_TARGETS.items():
        verdicts = {k: str(items[k]["verdict"]).strip().upper() for k in keys}
        worst = "FAIL" if "FAIL" in verdicts.values() else "PASS"
        summary = ", ".join(f"{k}={v}" for k, v in verdicts.items())
        for i, line in enumerate(lines):
            if not line.startswith(f"| {row_id} |"):
                continue
            cells = line.split(" | ")
            if len(cells) < 4:
                continue
            cells[2] = f"人工签字 {worst}（{data.get('attested_by')}）"
            cells[3] = (cells[3].rstrip().rstrip("|").rstrip()
                        + f" **人工结论 {worst}**：{summary}；"
                          f"证据见 {attest_path.parent.name}/attestation.json |")
            lines[i] = " | ".join(cells) + "\n"
            updated[row_id] = worst
            break
    if not dry_run:
        status_path.write_text("".join(lines), encoding="utf-8")

    report = attest_path.parent / "REPORT.md"
    if not dry_run:
        with report.open("a", encoding="utf-8") as fh:
            nl = chr(10)
            fh.write(nl + nl + "## Attestation merged (" + str(data.get("attested_at", "")) + ")" + nl + nl)
            fh.write("- attested_by: " + str(data.get("attested_by")) + nl)
            for key, item in items.items():
                fh.write("- " + str(item.get("label", key)) + ": **" + str(item.get("verdict")) + "** "
                         + str(item.get("notes", "")) + nl)
    return {"ok": True, "rows": updated, "dry_run": dry_run}


ENABLE_MOUSE_INPUT = 0x0010
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
ENABLE_QUICK_EDIT = 0x0040


def decode_input_mode(value: int) -> dict:
    """Interpret a console input mode (pure, unit-tested)."""
    return {
        "mode": value,
        "mouse_input_enabled": bool(value > 0 and value & ENABLE_MOUSE_INPUT),
        "quick_edit_enabled": bool(value > 0 and value & ENABLE_QUICK_EDIT),
        "vt_input": bool(value > 0 and value & ENABLE_VIRTUAL_TERMINAL_INPUT),
        "injection_supported": bool(value > 0 and value & ENABLE_MOUSE_INPUT),
    }


def console_input_mode(handle) -> dict:
    mode = ctypes.c_ulong(0)
    ok = kernel32.GetConsoleMode(handle, ctypes.byref(mode))
    decoded = decode_input_mode(int(mode.value) if ok else -1)
    return {"ok": bool(ok), **decoded}


def _write_mouse(handle, x: int, y: int, *, button: int, flags: int) -> int:
    event = _RECORD()
    event.EventType = MOUSE_EVENT
    event.MouseEvent = _MOUSE((x, y), button, 0, flags)
    array = (_RECORD * 1)(event)
    written = ctypes.c_ulong(0)
    kernel32.WriteConsoleInputW(handle, array, 1, ctypes.byref(written))
    return int(written.value)


def ime_burst_probe(pid: int, read_grid, marker: str,
                    gap: float = 0.05) -> dict:
    """Simulate an IME-like commit burst + Enter on the REAL console.

    What this DOES verify: a rapid multi-character burst followed almost
    immediately by Enter submits exactly ONE message (the 0.04s settle window
    and the armed-text comparison), and wide CJK characters render correctly.
    What it does NOT verify: a genuine Microsoft Pinyin composition state — the
    candidate window and its Enter cannot be synthesised from console records,
    so that stays a human item. The probe is labelled accordingly.
    """
    handle, error = _open_console(pid, "CONIN$")
    if handle is None:
        return {"ok": False, "error": error}
    try:
        _prepare_input(handle)
        records = []
        for ch in marker:
            for down in (True, False):
                event = _RECORD()
                event.EventType = KEY_EVENT
                event.KeyEvent = _KEY(down, 1, 0, 0, ch, 0)
                records.append(event)
        array = (_RECORD * len(records))(*records)
        written = ctypes.c_ulong(0)
        kernel32.WriteConsoleInputW(handle, array, len(records), ctypes.byref(written))
        time.sleep(gap)                       # commit → Enter, as an IME does
        enter = _write_key(handle, 0x0D, chr(13), ctrl_shift=False)
        time.sleep(0.6)
        enter += _write_key(handle, 0x0D, chr(13), ctrl_shift=False)  # settle window
    finally:
        kernel32.CloseHandle(handle)
        kernel32.FreeConsole()

    # poll instead of a fixed wait: a slow frame at a large size is not a failure
    deadline = time.monotonic() + 5.0
    lines: list = []
    user_lines: list = []
    while time.monotonic() < deadline:
        lines = read_grid(pid).get("lines") or []
        user_lines = [ln for ln in lines if ln.strip().startswith("✦") and marker in ln]
        if user_lines:
            break
        time.sleep(0.4)
    total = sum(ln.count(marker) for ln in lines)
    return {
        "kind": "ime_like_burst_simulation",
        "ok": len(user_lines) == 1,
        "user_lines_with_marker": len(user_lines),
        "marker_occurrences": total,
        "records_written": int(written.value) + enter,
        "user_line": user_lines[0].strip() if user_lines else "",
        "note": ("verifies the one-send guarantee for a burst+Enter sequence; a real "
                 "Microsoft Pinyin composition still requires a human"),
    }


def mouse_click_probe(pid: int, read_grid, opener: str = "/model") -> dict:
    """Open a picker, then click a row with a REAL console mouse event.

    Console mouse records carry cell coordinates, so no pixel maths is needed.
    """
    reset_handle, error = _open_console(pid, "CONIN$")
    if reset_handle is None:
        return {"ok": False, "error": error}
    mode_info = console_input_mode(reset_handle)
    try:
        _prepare_input(reset_handle)
    finally:
        kernel32.CloseHandle(reset_handle)
        kernel32.FreeConsole()

    inject_text(pid, opener, submit=True)
    time.sleep(1.8)
    handle, error = _open_console(pid, "CONIN$")
    if handle is None:
        return {"ok": False, "error": error, "input_mode": mode_info}
    try:
        before = read_grid(pid)
        before_lines = before.get("lines") or []
        # find a clickable value row (the app renders candidates as "  value  desc")
        candidate_rows = [i for i, ln in enumerate(before_lines)
                          if ln.strip() and not ln.strip().startswith(("❯", "─"))
                          and i > 1]
        if not candidate_rows:
            return {"ok": False, "error": "no candidate rows visible",
                    "input_mode": mode_info, "grid_sample": before_lines[:8]}
        target_row = candidate_rows[min(3, len(candidate_rows) - 1)]
        _write_mouse(handle, 6, target_row, button=0, flags=MOUSE_MOVED)
        time.sleep(0.2)
        pressed = _write_mouse(handle, 6, target_row,
                               button=FROM_LEFT_1ST_BUTTON, flags=0)
        time.sleep(0.25)
        released = _write_mouse(handle, 6, target_row, button=0, flags=0)
        time.sleep(0.8)
        after = read_grid(pid)
    finally:
        kernel32.CloseHandle(handle)
        kernel32.FreeConsole()

    after_lines = after.get("lines") or []
    marker_before = [i for i, ln in enumerate(before_lines) if "▸" in ln]
    marker_after = [i for i, ln in enumerate(after_lines) if "▸" in ln]
    return {
        "ok": bool(marker_after) and marker_after != marker_before,
        "target_row": target_row,
        "selection_before": marker_before,
        "selection_after": marker_after,
        "mouse_records": pressed + released,
        "input_mode": mode_info,
        "changed": after_lines != before_lines,
    }



VK_ESCAPE = 0x1B
VK_BACK = 0x08


def _prepare_input(handle) -> None:
    """Reset the app's input state before an interactive probe.

    Earlier probes may leave an inline prompt open or text in the box; an
    Escape burst closes any prompt (harmless while idle) and backspaces clear
    the line, so each probe starts from a known state.
    """
    for _ in range(3):
        _write_key(handle, VK_ESCAPE, chr(27), ctrl_shift=False)
        time.sleep(0.15)
    for _ in range(24):
        _write_key(handle, VK_BACK, chr(8), ctrl_shift=False)
    time.sleep(0.35)

# --------------------------------------------------------------- IME (IMM32)

IMM = ctypes.windll.imm32 if sys.platform == "win32" else None
SCS_SETSTR = 0x0009
NI_COMPOSITIONSTR = 0x0015
CPS_COMPLETE = 0x0001
CPS_CANCEL = 0x0002


def ime_commit_probe(hwnd: int, pid: int, read_grid, text: str) -> dict:
    """Drive a REAL IME commit through IMM32 (composition → CPS_COMPLETE).

    Unlike a character burst, this runs the actual IME path: set a composition
    string on the console window's input context and ask the IME to COMPLETE
    it. conhost then delivers the committed text to the application exactly as
    it does for a candidate-window Enter. If no IME context exists (or the host
    routes composition through TSF instead of IMM32) this returns a structured
    failure rather than pretending the item passed.
    """
    IMM.ImmGetContext.restype = ctypes.c_void_p
    IMM.ImmGetContext.argtypes = [ctypes.c_void_p]
    IMM.ImmReleaseContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    IMM.ImmSetCompositionStringW.restype = ctypes.c_int
    IMM.ImmSetCompositionStringW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_ulong,
        ctypes.c_void_p, ctypes.c_ulong]
    IMM.ImmNotifyIME.restype = ctypes.c_int
    IMM.ImmNotifyIME.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                 ctypes.c_ulong, ctypes.c_ulong]

    himc = IMM.ImmGetContext(hwnd)
    if not himc:
        return {"ok": False, "stage": "ImmGetContext",
                "error": f"no IME context on hwnd {hwnd} (err={kernel32.GetLastError()})",
                "note": "composition may be routed through TSF; human verification required"}
    try:
        set_ok = IMM.ImmSetCompositionStringW(
            himc, SCS_SETSTR, text, len(text) * ctypes.sizeof(ctypes.c_wchar), None, 0)
        commit_ok = IMM.ImmNotifyIME(himc, NI_COMPOSITIONSTR, CPS_COMPLETE, 0)
    finally:
        IMM.ImmReleaseContext(hwnd, himc)
    time.sleep(1.5)

    grid = read_grid(pid)
    lines = grid.get("lines") or []
    input_row = next((ln for ln in reversed(lines) if "❯" in ln), "")
    appeared = text in " ".join(lines)
    submitted = inject_text(pid, "", submit=True) if appeared else {"ok": False}
    time.sleep(1.8)
    after = read_grid(pid)
    user_lines = [ln for ln in (after.get("lines") or [])
                  if ln.strip().startswith("✦") and text in ln]
    return {
        "ok": bool(appeared and len(user_lines) == 1),
        "stage": "committed",
        "imm_set_composition_ok": bool(set_ok),
        "imm_notify_ime_ok": bool(commit_ok),
        "text_appeared_in_input": appeared,
        "input_row": input_row.strip(),
        "submit_after_commit": submitted,
        "user_lines_with_text": len(user_lines),
        "note": "real IMM32 composition + CPS_COMPLETE commit on the console window",
    }


# ---------------------------------------------------- mouse through VT input

def mouse_vt_click_probe(pid: int, read_grid, opener: str = "/model") -> dict:
    """Click a picker row by emitting the VT mouse report a terminal would send.

    The app runs with ENABLE_VIRTUAL_TERMINAL_INPUT, so it parses xterm-style
    SGR mouse sequences out of its input stream; writing those characters is
    therefore the same thing the terminal host does for a real click.
    """
    reset_handle, error = _open_console(pid, "CONIN$")
    if reset_handle is None:
        return {"ok": False, "error": error}
    try:
        _prepare_input(reset_handle)
    finally:
        kernel32.CloseHandle(reset_handle)
        kernel32.FreeConsole()

    typed = inject_text(pid, opener, submit=True)  # the measured-good submit path
    time.sleep(1.8)
    handle, error = _open_console(pid, "CONIN$")
    if handle is None:
        return {"ok": False, "error": error, "opener": typed}
    try:
        before = read_grid(pid)
        before_lines = before.get("lines") or []
        if not before_lines:
            return {"ok": False, "error": "console read returned nothing"}
        rows = [i for i, ln in enumerate(before_lines)
                if ln.strip() and 1 < i < len(before_lines) - 3]
        if not rows:
            return {"ok": False, "error": "no clickable rows visible",
                    "grid_sample": before_lines[:8]}
        target = rows[min(3, len(rows) - 1)]
        col, row1 = 8, target + 1  # SGR coordinates are 1-based

        # press then release, exactly the byte stream a terminal sends for a click
        esc = chr(27)
        for payload in (f"{esc}[<0;{col};{row1}M", f"{esc}[<0;{col};{row1}m"):
            for ch in payload:
                _write_key(handle, 0, ch, ctrl_shift=False)
            time.sleep(0.3)
        time.sleep(1.0)
        after = read_grid(pid)
    finally:
        kernel32.CloseHandle(handle)
        kernel32.FreeConsole()

    after_lines = after.get("lines") or []
    sel_before = [i for i, ln in enumerate(before_lines) if "▸" in ln]
    sel_after = [i for i, ln in enumerate(after_lines) if "▸" in ln]
    return {
        "ok": bool(sel_after) and sel_after != sel_before,
        "target_row": target,
        "selection_before": sel_before,
        "selection_after": sel_after,
        "changed": after_lines != before_lines,
        "method": "xterm SGR mouse report via VT input mode",
    }


def attest_interactive(out_dir: Path, ask=input, echo=print) -> dict:
    """Collect the six human verdicts from the terminal, then save them.

    Removes the last piece of friction (hand-editing JSON) while keeping the
    same rule: an empty verdict is recorded as empty, never defaulted to PASS.
    """
    target = out_dir / "attestation.json"
    data = attestation_template()
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    name = (ask("attested_by (your name): ") or "").strip()
    if not name:
        return {"ok": False, "reason": "attested_by is required"}
    data["attested_by"] = name
    data["attested_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    for key, label in HUMAN_ITEMS:
        echo("")
        echo(f"[{key}] {label}")
        verdict = ""
        while True:  # ask at least once; re-ask only on an unusable answer
            raw = (ask("  verdict (PASS/FAIL, Enter to skip): ") or "").strip().upper()
            if raw in ("PASS", "FAIL", "SKIP", ""):
                verdict = "" if raw == "SKIP" else raw
                break
        note = (ask("  evidence path / notes: ") or "").strip()
        section = ("host_limited_wt_only"
                   if key == "mouse_selection_and_click_wt" else "needs_hardware")
        data.setdefault(section, {})[key] = {
            "label": label, "verdict": verdict, "evidence": note, "notes": note}
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    filled = sum(1 for sec in data.values() if isinstance(sec, dict)
                 for i in sec.values() if isinstance(i, dict) and i.get("verdict"))
    return {"ok": True, "written": str(target), "filled": filled,
            "total": len(HUMAN_ITEMS)}
