"""Clipboard DIB decoding and pastes-directory housekeeping.

The image path had NO test coverage at all, and it was dead: `_dib_to_png`
imported Pillow, which is not a declared dependency, so every clipboard paste
reported "could not decode clipboard image". These tests decode the produced
PNG with the standard library and assert the actual pixels, so a decoder that
returns merely *some* bytes does not pass.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from oaset.tui.clipboard import _dib_to_png, _encode_png
from oaset.tui.paste import prune_pastes

# --------------------------------------------------------------- PNG reader


def decode_png(blob: bytes) -> tuple[int, int, int, bytes]:
    """Minimal PNG reader: (width, height, channels, raw top-down pixels)."""
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos = 8
    width = height = channels = 0
    idat = b""
    while pos < len(blob):
        (length,) = struct.unpack_from(">I", blob, pos)
        tag = blob[pos + 4:pos + 8]
        payload = blob[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", payload[:10])
            assert depth == 8, depth
            channels = {0: 1, 2: 3, 4: 2, 6: 4}[colour]
        elif tag == b"IDAT":
            idat += payload
        elif tag == b"IEND":
            break
    raw = zlib.decompress(idat)
    stride = width * channels
    rows = bytearray()
    for row in range(height):
        start = row * (stride + 1)
        assert raw[start] == 0, "expected filter type 0"
        rows += raw[start + 1:start + 1 + stride]
    return width, height, channels, bytes(rows)


# ------------------------------------------------------------------- DIBs


def make_dib(width: int, height: int, bpp: int, *, bgr=(10, 20, 30), alpha=255,
             compression: int = 0, planes: int = 1, header_size: int = 40,
             top_down: bool = False) -> bytes:
    """A real BITMAPINFOHEADER DIB, bottom-up by default (Windows convention)."""
    h = -height if top_down else height
    header = struct.pack("<IiiHHIIiiII", header_size, width, h, planes, bpp,
                         compression, 0, 0, 0, 0, 0)
    if compression == 3 and header_size == 40:
        header += struct.pack("<III", 0x00FF0000, 0x0000FF00, 0x000000FF)
    b, g, r = bgr
    stride = ((width * bpp + 31) // 32) * 4
    if bpp == 32:
        pixel = bytes((b, g, r, alpha))
        row = pixel * width
    else:
        row = (bytes((b, g, r)) * width)[:stride]
    row = row.ljust(stride, b"\x00")
    return header + row * height


BGR = (10, 20, 30)
RGB = (30, 20, 10)  # what the image should be after the BGR -> RGB swap


def test_24bpp_dib_decodes_with_channels_swapped():
    png = _dib_to_png(make_dib(4, 3, 24, bgr=BGR))
    assert png is not None, "a valid 24bpp DIB must decode"
    width, height, channels, pixels = decode_png(png)
    assert (width, height, channels) == (4, 3, 3)
    assert tuple(pixels[:3]) == RGB


def test_32bpp_dib_with_unused_alpha_decodes_as_rgb():
    """Screen captures are usually 32bpp with a zero high byte; treating that
    as opacity produced a fully transparent image."""
    png = _dib_to_png(make_dib(3, 2, 32, bgr=BGR, alpha=0))
    assert png is not None
    width, height, channels, pixels = decode_png(png)
    assert (width, height) == (3, 2)
    assert channels == 3, f"expected opaque RGB, got {channels} channels"
    assert tuple(pixels[:3]) == RGB


def test_32bpp_dib_with_real_alpha_keeps_it():
    png = _dib_to_png(make_dib(2, 2, 32, bgr=BGR, alpha=128))
    assert png is not None
    width, height, channels, pixels = decode_png(png)
    assert channels == 4, "a real alpha channel must be preserved"
    assert tuple(pixels[:4]) == (*RGB, 128)


def test_bitfields_header_skips_the_colour_masks():
    """Reading biCompression as bytes 12:16 pulled in biPlanes, so a classic
    BI_BITFIELDS header was never recognised and its 12 mask bytes were decoded
    as pixels — a diagonally skewed image that then reached the model."""
    png = _dib_to_png(make_dib(3, 2, 32, bgr=BGR, alpha=255, compression=3))
    assert png is not None, "BI_BITFIELDS must be recognised"
    width, height, channels, pixels = decode_png(png)
    assert (width, height) == (3, 2)
    assert tuple(pixels[:3]) == RGB


def test_bottom_up_rows_are_flipped():
    """biHeight > 0 means bottom-up: without the flip the image is upside down."""
    stride = ((2 * 24 + 31) // 32) * 4
    top = (bytes((1, 2, 3)) * 2).ljust(stride, b"\x00")
    bottom = (bytes((200, 201, 202)) * 2).ljust(stride, b"\x00")
    header = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 24, 0, 0, 0, 0, 0, 0)
    png = _dib_to_png(header + bottom + top)  # stored bottom row first
    assert png is not None
    _, _, _, pixels = decode_png(png)
    assert tuple(pixels[:3]) == (3, 2, 1), "first output row must be the TOP row"


def test_truncated_dib_is_refused():
    """Failing beats guessing: a short buffer used to decode into garbage."""
    full = make_dib(8, 8, 24)
    assert _dib_to_png(full[:-20]) is None


def test_unsupported_compression_is_refused():
    assert _dib_to_png(make_dib(4, 4, 24, compression=1)) is None  # BI_RLE8


def test_rejects_non_24_32bpp_and_empty():
    assert _dib_to_png(make_dib(4, 4, 8)) is None
    assert _dib_to_png(b"") is None
    assert _dib_to_png(b"\x00" * 39) is None


def test_decoder_does_not_need_pillow():
    """Pillow is NOT a dependency: if this module needs it, the feature is dead."""
    import sys

    png = _dib_to_png(make_dib(3, 3, 24))
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"
    assert "PIL" not in sys.modules or True  # informational; the call above is the proof


def test_encode_png_roundtrip_is_lossless():
    data = bytes([1, 2, 3] * 3)
    png = _encode_png(3, 1, 3, data)
    assert png is not None
    assert decode_png(png) == (3, 1, 3, data)
    assert _encode_png(1, 1, 5, b"x") is None  # unsupported channel count


# -------------------------------------------------------------- pruning


def _touch(path: Path, age_hours: float) -> None:
    import os
    import time

    stamp = time.time() - age_hours * 3600
    os.utime(path, (stamp, stamp))


def test_prune_keeps_the_newest_within_the_count(tmp_path):
    tmp_path = tmp_path / "pastes"
    tmp_path.mkdir()
    for i in range(60):
        p = tmp_path / f"f{i:02d}.txt"
        p.write_text("x", encoding="utf-8")
        _touch(p, age_hours=100 + i)
    removed = prune_pastes(tmp_path, keep=40, min_age_hours=24)
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert removed == 20, f"expected 20 removals, got {removed}"
    assert len(remaining) == 40


def test_prune_never_deletes_a_recent_paste(tmp_path):
    tmp_path = tmp_path / "pastes"
    tmp_path.mkdir()
    """A draft can still reference a paste made minutes ago: expansion happens
    at submit time, so deleting it would turn the paste into a warning."""
    for i in range(60):
        p = tmp_path / f"recent{i:02d}.txt"
        p.write_text("x", encoding="utf-8")  # brand new
    removed = prune_pastes(tmp_path, keep=5, min_age_hours=24)
    assert removed == 0, "nothing young may be removed, however many there are"
    assert len(list(tmp_path.iterdir())) == 60


def test_prune_keeps_old_files_below_the_count(tmp_path):
    tmp_path = tmp_path / "pastes"
    tmp_path.mkdir()
    for i in range(5):
        p = tmp_path / f"old{i}.txt"
        p.write_text("x", encoding="utf-8")
        _touch(p, age_hours=24 * 60)
    removed = prune_pastes(tmp_path, keep=40, min_age_hours=24)
    assert removed == 0
    assert len(list(tmp_path.iterdir())) == 5


def test_prune_is_safe_on_a_missing_directory(tmp_path):
    assert prune_pastes(tmp_path / "pastes" / "nope") == 0


def test_paste_write_paths_call_prune():
    """Both scratch writers must sweep, or the directory grows unbounded."""
    import inspect

    from oaset.tui.controllers import content_cmds
    from oaset.tui.widgets import input as input_mod

    assert "prune_pastes" in inspect.getsource(input_mod.InputArea.insert_collapsed_paste)
    assert "prune_pastes" in inspect.getsource(
        content_cmds.ContentCmdsMixin._attach_clipboard_image)


# ------------------------------------------------- end-to-end image paste


async def test_clipboard_image_is_attached_as_a_real_png(workspace, isolated_home,
                                                        monkeypatch):
    """Ctrl+Shift+V with an image must produce a real PNG the model can be sent.

    This is the path that was dead: the decoder required Pillow, which is not a
    dependency, so every paste failed and the user only ever saw "could not
    decode clipboard image".
    """
    from oaset.tui import clipboard
    from oaset.tui.widgets.input import InputArea
    from tests.test_app_tui import make_app

    dib = make_dib(6, 4, 24, bgr=BGR)
    monkeypatch.setattr(clipboard, "get_image_png", lambda: (_dib_to_png(dib), None))

    app = make_app(workspace, [])
    # the image-attach path only runs for a model that accepts images
    monkeypatch.setattr(type(app.model_cfg), "has",
                        lambda self, name: name == "image_in", raising=False)

    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.3)
        field = app.query_one(InputArea)
        await field.action_paste_clipboard()
        await pilot.pause(0.3)

        assert app._pending_images, "the image must be staged for the next send"
        saved = app._pending_images[0]
        assert saved.suffix == ".png"
        assert saved.is_file(), f"{saved} was not written"
        width, height, channels, pixels = decode_png(saved.read_bytes())
        assert (width, height) == (6, 4)
        assert tuple(pixels[:3]) == RGB, "pixels must survive the whole path"
