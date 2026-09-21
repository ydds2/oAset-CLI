"""Image attachments (multi-part content) and custom themes."""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

from oaset.agent import AgentLoop, Conversation
from oaset.agent.messages import Message
from oaset.providers import MockProvider, MockTurn
from oaset.tools import AutoGate, ToolContext, ToolRegistry
from oaset.tui.themes import load_custom_themes, normalize_theme
from oaset.utils import extract_image_attachments


def make_png(path: Path) -> None:
    """Write a tiny valid 1x1 PNG without external dependencies."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def test_extract_image_attachments(workspace):
    make_png(workspace / "shot.png")
    (workspace / "notes.txt").write_text("not an image", encoding="utf-8")
    parts = extract_image_attachments("look at shot.png please", workspace)
    assert len(parts) == 1
    assert parts[0]["type"] == "image_url"
    url = parts[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    base64.b64decode(url.split(",", 1)[1])  # decodes
    assert extract_image_attachments("see notes.txt", workspace) == []
    assert extract_image_attachments("missing.png here", workspace) == []
    assert extract_image_attachments("shot.png", workspace, enabled=False) == []


def test_message_multipart_content():
    content = [
        {"type": "text", "text": "look at this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    msg = Message(role="user", content=content)
    wire = msg.to_wire()
    assert wire["content"] == content  # passed through untouched
    assert msg.display_text() == "look at this"
    restored = Message.from_dict(msg.to_dict())
    assert restored.content == content


def test_conversation_tokens_with_images():
    conv = Conversation(system_prompt=None)
    conv.append(
        Message(role="user", content=[
            {"type": "text", "text": "abcd"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ])
    )
    assert conv.token_estimate() >= 750


async def test_loop_sends_multipart_user_message(workspace):
    make_png(workspace / "pic.png")
    provider = MockProvider([MockTurn(content_chunks=["got the picture"])])
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=200))
    loop = AgentLoop(provider, registry, Conversation(system_prompt="s"))
    images = extract_image_attachments("see pic.png", workspace)
    await loop.run("see pic.png", lambda e: None, images=images)
    first_user = provider.requests[0][1]  # [0] is the system message
    assert isinstance(first_user["content"], list)
    assert first_user["content"][0] == {"type": "text", "text": "see pic.png"}
    assert first_user["content"][1]["type"] == "image_url"


# -------------------------------------------------------------------- themes


def test_builtin_themes_leave_the_terminal_background():
    """The product theme must not paint a solid fill over the console.

    Textual's CSS ``transparent`` becomes ``#000000`` (still a fill). The colour
    that actually defers to the terminal is ``ansi_default``.
    """
    from oaset.tui.themes import OASET_DARK, OASET_LIGHT, TERMINAL_BG

    assert TERMINAL_BG == "ansi_default"
    for theme in (OASET_DARK, OASET_LIGHT):
        assert theme.background == TERMINAL_BG
        assert theme.surface == TERMINAL_BG
        assert theme.panel == TERMINAL_BG
        generated = theme.to_color_system().generate()
        assert generated["background"] == "ansi_default"
        assert generated["surface"] == "ansi_default"
        assert generated["panel"] == "ansi_default"
        assert "#0d0d0d" not in generated.values()
        assert "#f7f7f7" not in generated.values()


def test_load_custom_themes(isolated_home):
    themes_dir = isolated_home / "themes"
    themes_dir.mkdir()
    (themes_dir / "nord.toml").write_text(
        'primary = "#88c0d0"\nbackground = "#2e3440"\nforeground = "#eceff4"\ndark = true\n',
        encoding="utf-8",
    )
    (themes_dir / "broken.toml").write_text("not toml [[[", encoding="utf-8")
    custom = load_custom_themes(isolated_home)
    assert set(custom) == {"nord"}
    assert custom["nord"].primary == "#88c0d0"
    assert normalize_theme("nord", extra=set(custom)) == "nord"
    assert normalize_theme("nord") == "oaset-dark"  # not registered → fallback
