"""Minimal BMP→PNG and integer scaling — no Pillow.

UIA captures 24-bit BMP. Vision models want PNG (BMP is often refused).
Uncompressed PNG (filter 0 + zlib) is enough for screenshots.
"""

from __future__ import annotations

import struct
import zlib

# Computer-use screenshot frame: pick the first target whose aspect
# matches and whose width is smaller than the physical display.
_SCALE_TARGETS = (
    (1024, 768),   # 4:3
    (1280, 800),   # 16:10
    (1366, 768),   # ~16:9
)


def screenshot_frame(width: int, height: int) -> tuple[int, int]:
    """The pixel size the model sees (and emits coordinates in)."""
    if width <= 0 or height <= 0:
        return 1280, 800
    ratio = width / height
    for tw, th in _SCALE_TARGETS:
        if abs(tw / th - ratio) < 0.03 and tw < width:
            return tw, th
    return width, height


def scale_up(sx: int, sy: int, frame: tuple[int, int], physical: tuple[int, int]) -> tuple[int, int]:
    """Screenshot-space → physical pixels."""
    fw, fh = frame
    pw, ph = physical
    if fw <= 0 or fh <= 0:
        return sx, sy
    return int(sx * pw / fw), int(sy * ph / fh)


def scale_down(px: int, py: int, frame: tuple[int, int], physical: tuple[int, int]) -> tuple[int, int]:
    """Physical pixels → screenshot-space (cursor_position)."""
    fw, fh = frame
    pw, ph = physical
    if pw <= 0 or ph <= 0:
        return px, py
    return int(px * fw / pw), int(py * fh / ph)


def decode_bmp(data: bytes) -> tuple[bytes, int, int]:
    """Return (RGB bytes, width, height) from a 24-bit BMP. Raises ValueError."""
    if len(data) < 54 or data[0:2] != b"BM":
        raise ValueError("not a BMP")
    off = struct.unpack_from("<I", data, 10)[0]
    width = struct.unpack_from("<i", data, 18)[0]
    height = struct.unpack_from("<i", data, 22)[0]
    bits = struct.unpack_from("<H", data, 28)[0]
    if bits != 24:
        raise ValueError(f"BMP bit count {bits} unsupported")
    top_down = height < 0
    height = abs(height)
    row_stride = (width * 3 + 3) & ~3
    rgb = bytearray(width * height * 3)
    for row in range(height):
        src_row = row if top_down else (height - 1 - row)
        src = off + src_row * row_stride
        dst = row * width * 3
        for col in range(width):
            b = data[src + col * 3]
            g = data[src + col * 3 + 1]
            r = data[src + col * 3 + 2]
            rgb[dst + col * 3] = r
            rgb[dst + col * 3 + 1] = g
            rgb[dst + col * 3 + 2] = b
    return bytes(rgb), width, height


def scale_rgb(rgb: bytes, width: int, height: int, tw: int, th: int) -> bytes:
    """Nearest-neighbour scale. Identity when the size is unchanged."""
    if tw == width and th == height:
        return rgb
    if tw <= 0 or th <= 0:
        return rgb
    out = bytearray(tw * th * 3)
    for y in range(th):
        sy = min(height - 1, y * height // th)
        src_row = sy * width * 3
        dst_row = y * tw * 3
        for x in range(tw):
            sx = min(width - 1, x * width // tw)
            s = src_row + sx * 3
            d = dst_row + x * 3
            out[d:d + 3] = rgb[s:s + 3]
    return bytes(out)


def crop_rgb(rgb: bytes, width: int, height: int, x0: int, y0: int, x1: int, y1: int) -> tuple[bytes, int, int]:
    x0 = max(0, min(width, x0))
    x1 = max(x0 + 1, min(width, x1))
    y0 = max(0, min(height, y0))
    y1 = max(y0 + 1, min(height, y1))
    cw, ch = x1 - x0, y1 - y0
    out = bytearray(cw * ch * 3)
    for y in range(ch):
        src = ((y0 + y) * width + x0) * 3
        dst = y * cw * 3
        out[dst:dst + cw * 3] = rgb[src:src + cw * 3]
    return bytes(out), cw, ch


def encode_png(rgb: bytes, width: int, height: int) -> bytes:
    """Uncompressed (zlib) 8-bit RGB PNG."""
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)  # filter None
        start = y * stride
        raw.extend(rgb[start:start + stride])
    compressed = zlib.compress(bytes(raw), 6)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        header = struct.pack(">I", len(payload)) + tag + payload
        return header + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


def screenshot_to_png(mime: str, data: bytes, width: int, height: int,
                      frame: tuple[int, int] | None = None) -> tuple[bytes, int, int]:
    """Normalise a capture to PNG in the model's coordinate frame.

    Unparseable payloads (test fakes) pass through unchanged.
    """
    rgb: bytes | None = None
    w, h = width, height
    if mime == "image/bmp" or (data[:2] == b"BM"):
        try:
            rgb, w, h = decode_bmp(data)
        except (ValueError, struct.error, IndexError):
            rgb = None
    if rgb is None:
        return data, width, height
    tw, th = frame or screenshot_frame(w, h)
    if (tw, th) != (w, h):
        rgb = scale_rgb(rgb, w, h, tw, th)
        w, h = tw, th
    return encode_png(rgb, w, h), w, h


def crop_screenshot_to_png(mime: str, data: bytes, width: int, height: int,
                           region: tuple[int, int, int, int],
                           frame: tuple[int, int]) -> bytes:
    """Zoom: crop `region` (screenshot pixels) and fit it into `frame`."""
    rgb, w, h = decode_bmp(data) if (mime == "image/bmp" or data[:2] == b"BM") else (None, 0, 0)
    if rgb is None:
        raise ValueError("zoom needs a BMP capture")
    x0, y0, x1, y1 = region
    cropped, cw, ch = crop_rgb(rgb, w, h, x0, y0, x1, y1)
    fw, fh = frame
    # fit inside the frame, aspect preserved (nearest neighbour)
    scale = min(fw / cw, fh / ch)
    tw, th = max(1, int(cw * scale)), max(1, int(ch * scale))
    fitted = scale_rgb(cropped, cw, ch, tw, th)
    return encode_png(fitted, tw, th)
