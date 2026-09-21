"""Local computer-use: Claude-compatible actions on a fake Windows desktop."""

from __future__ import annotations

import json

from oaset.computer.image import encode_png, scale_up, screenshot_frame, screenshot_to_png
from oaset.computer.input import FakeInputDriver
from oaset.computer.use import ComputerUseSession
from oaset.tools.base import ToolContext, ToolResult
from oaset.tools.computer_use import BashTool, ComputerTool, TextEditorTool
from tests.test_desktop_provider import AllowGate, FakeUiaBackend


def _session(backend=None, driver=None) -> ComputerUseSession:
    backend = backend or FakeUiaBackend()
    driver = driver or FakeInputDriver(width=1920, height=1080)
    return ComputerUseSession(backend, driver=driver, gate=AllowGate())


def test_png_roundtrip_and_frame_scaling():
    rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0])  # 2x2
    png = encode_png(rgb, 2, 2)
    assert png.startswith(b"\x89PNG")
    assert screenshot_frame(1920, 1080) == (1366, 768)
    assert screenshot_frame(1280, 800) == (1280, 800)
    px, py = scale_up(683, 384, (1366, 768), (1920, 1080))
    assert abs(px - 960) < 3 and abs(py - 540) < 3
    # unparseable fake PNG passes through
    out, w, h = screenshot_to_png("image/png", b"\x89PNG-fake", 1920, 1080)
    assert out == b"\x89PNG-fake" and w == 1920


async def test_screenshot_click_type_key_scroll_drag():
    driver = FakeInputDriver(width=1920, height=1080)
    session = _session(driver=driver)
    shot = await session.act({"action": "screenshot"})
    assert not shot.is_error and shot.png
    # FakeUiaBackend captures 2400×1350; non-BMP payloads are not rescaled.
    assert shot.width == 2400 and shot.height == 1350
    moved = await session.act({"action": "mouse_move", "coordinate": [100, 50]})
    assert not moved.is_error
    # 100,50 in the 2400×1350 screenshot frame → physical 1920×1080
    assert any(e[0] == "move" for e in driver.log)
    mx, my = next((e[1], e[2]) for e in driver.log if e[0] == "move")
    assert mx == scale_up(100, 50, session.frame, session.physical)[0]
    assert my == scale_up(100, 50, session.frame, session.physical)[1]
    clicked = await session.act({"action": "left_click", "coordinate": [10, 20]})
    assert not clicked.is_error
    assert any(e[0] == "click" and e[1] == "left" for e in driver.log)
    await session.act({"action": "type", "text": "hello"})
    assert ("type", "hello") in driver.log
    await session.act({"action": "key", "text": "ctrl+c"})
    assert ("key", "ctrl+c", 1) in driver.log
    await session.act({"action": "scroll", "scroll_direction": "down", "scroll_amount": 3})
    assert any(e[0] == "scroll" and e[2] == -3 for e in driver.log)
    dragged = await session.act({
        "action": "left_click_drag",
        "start_coordinate": [1, 1],
        "coordinate": [40, 40],
    })
    assert not dragged.is_error
    assert any(e == ("button", "left", "down") for e in driver.log)
    wait = await session.act({"action": "wait", "duration": 0})
    assert not wait.is_error
    pos = await session.act({"action": "cursor_position"})
    assert pos.output.startswith("X=")
    bad = await session.act({"action": "not_a_thing"})
    assert bad.is_error


async def test_computer_tool_returns_image_on_screenshot():
    ctx = ToolContext(cwd=__import__("pathlib").Path("."))
    ctx.session_state["desktop_backend"] = FakeUiaBackend()
    ctx.session_state["input_driver"] = FakeInputDriver(width=1920, height=1080)
    ctx.gate = AllowGate()
    result = await ComputerTool().run({"action": "screenshot"}, ctx)
    assert not result.is_error
    assert result.image and result.image_mime == "image/png"


async def test_bash_and_editor_map_to_local_tools(tmp_path):
    ctx = ToolContext(cwd=tmp_path, mode="auto", output_limit=2000)
    (tmp_path / "a.txt").write_text("hello world\n", encoding="utf-8")
    view = await TextEditorTool().run({"command": "view", "path": "a.txt"}, ctx)
    assert "hello world" in view.output
    created = await TextEditorTool().run(
        {"command": "create", "path": "b.txt", "file_text": "new\n"}, ctx)
    assert not created.is_error and (tmp_path / "b.txt").read_text(encoding="utf-8") == "new\n"
    replaced = await TextEditorTool().run(
        {"command": "str_replace", "path": "a.txt",
         "old_str": "world", "new_str": "oaset"}, ctx)
    assert not replaced.is_error
    assert "hello oaset" in (tmp_path / "a.txt").read_text(encoding="utf-8")
    inserted = await TextEditorTool().run(
        {"command": "insert", "path": "a.txt", "insert_line": 1, "insert_text": "line2"}, ctx)
    assert not inserted.is_error
    bash = await BashTool().run({"restart": True}, ctx)
    assert "restarted" in bash.output
    empty = await BashTool().run({}, ctx)
    assert empty.is_error


async def test_loop_attaches_screenshot_to_tool_message(tmp_path):
    from oaset.agent.loop import AgentLoop
    from oaset.agent.messages import Conversation, ToolCallReq
    from oaset.tools import AutoGate, Tool, ToolRegistry
    from oaset.tools.base import EXEC

    class ShotTool(Tool):
        name = "computer"
        permission = EXEC
        required: list[str] = []
        parameters: dict = {}

        async def run(self, args, ctx):
            return ToolResult("screenshot 2x2", image=encode_png(bytes(12), 2, 2))

    registry = ToolRegistry(tools=[], gate=AutoGate())
    registry.add_tool(ShotTool())
    registry.bind(ToolContext(cwd=tmp_path, mode="auto"))
    loop = AgentLoop(provider=None, registry=registry, conversation=Conversation())
    events: list[dict] = []
    await loop._execute_tool(
        ToolCallReq(id="c1", name="computer", arguments='{"action":"screenshot"}'),
        events.append,
    )
    tool_msg = [m for m in loop.conversation.messages if m.role == "tool"][0]
    assert isinstance(tool_msg.content, list)
    kinds = [p["type"] for p in tool_msg.content]
    assert "text" in kinds and "image_url" in kinds
    url = next(p["image_url"]["url"] for p in tool_msg.content if p["type"] == "image_url")
    assert url.startswith("data:image/png;base64,")


def test_anthropic_tool_content_splits_image():
    from oaset.providers.anthropic_native import _anthropic_tool_content

    blocks = _anthropic_tool_content([
        {"type": "text", "text": "shot"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
    ])
    assert blocks[0] == {"type": "text", "text": "shot"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["data"] == "QUJD"


async def test_anthropic_keeps_local_computer_use_as_ordinary_tools():
    import httpx

    from oaset.providers.anthropic_native import AnthropicNativeProvider

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        captured["beta"] = request.headers.get("anthropic-beta", "")
        return httpx.Response(200, content=b"", headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicNativeProvider("anthropic/claude-sonnet", api_key="sk", client=client)
    provider.native_skills = frozenset({"computer", "bash", "text_editor"})
    tools = [
        {"type": "function", "function": {"name": "computer", "parameters": {}}},
        {"type": "function", "function": {"name": "read_file", "parameters": {}}},
        {"type": "function", "function": {"name": "bash", "parameters": {}}},
    ]
    async for _ in provider.stream([{"role": "user", "content": "hi"}], tools):
        pass
    names = [t.get("name") for t in captured["body"]["tools"]]
    types = {t.get("type") for t in captured["body"]["tools"]}
    assert names.count("computer") == 1
    assert names.count("bash") == 1
    assert "read_file" in names
    # Local computer-use goes out as ORDINARY tools (the Anthropic tool
    # format wraps nothing in "type"): no vendor computer-use protocol and
    # no beta header (owner decision 2026-09-16).
    assert not any(str(t).startswith(("computer_", "bash_", "text_editor_"))
                   for t in types if t)
    assert "computer-use-2025-11-24" not in captured["beta"]
    plain = [t for t in captured["body"]["tools"]
             if t.get("name") in ("computer", "bash")]
    assert len(plain) == 2 and all("input_schema" in t for t in plain)


def test_default_registry_includes_computer_use_tools():
    from oaset.tools import ToolRegistry

    registry = ToolRegistry(gate=None)
    for name in ("computer", "bash", "str_replace_based_edit_tool"):
        assert name in registry.tools, name
