"""Small, dependency-free clipboard adapter for the TUI.

Windows uses user32/kernel32 directly so Ctrl+Shift+C/V works in hosts that
do not turn terminal key sequences into Textual Paste events. Other systems
return a structured unavailable result; Textual's native Paste event remains
usable there.
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ClipboardError:
    code: str
    message: str


def describe(error: ClipboardError) -> tuple[str, str]:
    """(localised text, notice level) for a clipboard failure."""
    from oaset.i18n import t

    if error.code == "clipboard_unavailable":
        return t("clipboard_unavailable"), "warn"
    if error.code == "clipboard_empty":
        return t("clipboard_empty"), "info"
    return t("clipboard_failed", reason=error.message), "warn"


def _win32():
    """user32/kernel32 with explicit prototypes.

    Without restype/argtypes, ctypes truncates returned HANDLEs to 32-bit ints
    on 64-bit Windows, so GlobalLock/SetClipboardData silently fail.
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.EmptyClipboard.restype = ctypes.c_int
    user32.CloseClipboard.restype = ctypes.c_int
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    return user32, kernel32


def _open_clipboard(user32) -> bool:
    """OpenClipboard with the same bounded retry set_text uses.

    A busy clipboard manager must not turn every read into a hard failure.
    """
    for attempt in range(5):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.015 * (attempt + 1))
    return False


def get_image_png() -> tuple[bytes | None, ClipboardError | None]:
    """PNG bytes from the Windows clipboard (CF_DIB → PNG), if any."""
    if os.name != "nt":
        return None, ClipboardError("clipboard_unavailable", "Windows clipboard is unavailable")
    user32, kernel32 = _win32()
    if not _open_clipboard(user32):
        return None, ClipboardError("clipboard_open_failed", "could not open clipboard")
    try:
        handle = user32.GetClipboardData(8)  # CF_DIB
        if not handle:
            return None, ClipboardError("clipboard_empty", "clipboard has no image")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None, ClipboardError("clipboard_read_failed", "could not read clipboard")
        try:
            nbytes = int(kernel32.GlobalSize(handle))
            if nbytes < 40:
                return None, ClipboardError("clipboard_empty", "clipboard image is empty")
            dib = ctypes.string_at(ptr, nbytes)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    png = _dib_to_png(dib)
    if not png:
        return None, ClipboardError("clipboard_read_failed", "could not decode clipboard image")
    return png, None


def _encode_png(width: int, height: int, channels: int, data: bytes) -> bytes | None:
    """Encode raw rows as a PNG using only the standard library.

    zlib already ships with Python, so nothing here needs Pillow. Rows arrive
    top-down; PNG wants a filter-type byte in front of each one.
    """
    import struct
    import zlib

    colour_type = {1: 0, 2: 4, 3: 2, 4: 6}.get(channels)
    if colour_type is None:
        return None

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    stride = width * channels
    raw = bytearray()
    for row in range(height):
        raw.append(0)  # filter type 0 (None)
        raw += data[row * stride:(row + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def _dib_to_png(dib: bytes) -> bytes | None:
    """Decode a Windows clipboard DIB (BITMAPINFOHEADER) into PNG bytes.

    Stdlib only, on purpose. This used to require Pillow, which is NOT a
    declared dependency of this project, so the import always failed and
    `_dib_to_png` returned None for every input — pasting an image with
    Ctrl+Shift+V silently reported "could not decode clipboard image" and the
    feature could never work in a normal install.

    Header layout (little-endian):
        biSize@0  biWidth@4  biHeight@8  biPlanes@12
        biBitCount@14  biCompression@16  biSizeImage@20
    Reading biCompression as bytes 12:16 pulled in the 2-byte biPlanes field,
    so a classic 40-byte BI_BITFIELDS header (3) was never recognised and its
    12 colour-mask bytes were decoded as pixels.
    """
    import struct

    if len(dib) < 40:
        return None
    header_size, width, height = struct.unpack_from("<Iii", dib, 0)
    bpp, compression = struct.unpack_from("<HH", dib, 14)  # NOT 12
    if header_size < 40 or width <= 0 or height == 0 or bpp not in (24, 32):
        return None
    if compression not in (0, 3):  # BI_RGB, BI_BITFIELDS
        return None
    # BI_BITFIELDS with a classic header carries three 4-byte colour masks
    # between the header and the pixels; V4/V5 headers keep them inside.
    pixel_offset = header_size
    if compression == 3 and header_size == 40:
        pixel_offset += 12
    stride = ((width * bpp + 31) // 32) * 4
    need = stride * abs(height)
    if len(dib) < pixel_offset + need:
        return None  # truncated: fail rather than guess a shifted image
    body = dib[pixel_offset:pixel_offset + need]
    bottom_up = height > 0

    src_channels = bpp // 8
    rows = []
    for y in range(abs(height)):
        src_row = (abs(height) - 1 - y) if bottom_up else y
        rows.append(body[src_row * stride:src_row * stride + width * src_channels])
    flat = b"".join(rows)

    bgr = bytearray(len(flat))
    bgr[0::src_channels] = flat[2::src_channels]  # B -> R
    bgr[1::src_channels] = flat[1::src_channels]  # G -> G
    bgr[2::src_channels] = flat[0::src_channels]  # R -> B

    if src_channels == 4:
        alpha = flat[3::4]
        # Most Windows screen captures are 32bpp with the high byte unused;
        # treating that as opacity produced fully transparent images. Keep
        # alpha only when the capture actually carries a meaningful one.
        if any(a != 0 for a in alpha):
            channels = 4
            bgr[3::4] = alpha
        else:
            channels = 3
            stripped = bytearray(width * abs(height) * 3)
            stripped[0::3] = bgr[0::4]
            stripped[1::3] = bgr[1::4]
            stripped[2::3] = bgr[2::4]
            bgr = stripped
    else:
        channels = 3

    return _encode_png(width, abs(height), channels, bytes(bgr))


def get_text() -> tuple[str | None, ClipboardError | None]:
    if os.name != "nt":
        return None, ClipboardError("clipboard_unavailable", "Windows clipboard is unavailable")
    user32, kernel32 = _win32()
    if not _open_clipboard(user32):
        return None, ClipboardError("clipboard_open_failed", "could not open clipboard")
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return None, ClipboardError("clipboard_empty", "clipboard has no text")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None, ClipboardError("clipboard_read_failed", "could not read clipboard")
        try:
            return ctypes.wstring_at(ptr), None
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_text(text: str) -> ClipboardError | None:
    if os.name != "nt":
        return ClipboardError("clipboard_unavailable", "Windows clipboard is unavailable")
    user32, kernel32 = _win32()
    # OpenClipboard fails while ANY other process holds the clipboard (a copy
    # in a browser, a clipboard manager). Brief bounded retries (P1-8) turn
    # that common race from a hard failure into a pause nobody notices.
    opened = False
    for attempt in range(5):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.015 * (attempt + 1))
    if not opened:
        return ClipboardError("clipboard_open_failed",
                              "clipboard busy after retries")
    try:
        if not user32.EmptyClipboard():
            return ClipboardError("clipboard_write_failed", "could not clear clipboard")
        data = ctypes.create_unicode_buffer(text + "\0")
        size = ctypes.sizeof(data)
        hmem = kernel32.GlobalAlloc(0x0002, size)  # GMEM_MOVEABLE
        if not hmem:
            return ClipboardError("clipboard_write_failed", "could not allocate clipboard memory")
        ptr = kernel32.GlobalLock(hmem)
        if not ptr:
            kernel32.GlobalFree(hmem)
            return ClipboardError("clipboard_write_failed", "could not lock clipboard memory")
        try:
            ctypes.memmove(ptr, ctypes.addressof(data), size)
        finally:
            kernel32.GlobalUnlock(hmem)
        if not user32.SetClipboardData(13, hmem):
            kernel32.GlobalFree(hmem)
            return ClipboardError("clipboard_write_failed", "could not set clipboard text")
        return None
    finally:
        user32.CloseClipboard()
