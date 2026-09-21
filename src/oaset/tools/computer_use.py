"""Local computer-use tools: protocol-compatible names, Windows execution.

``computer``           — screenshot / click / type / key / scroll / drag / zoom
``bash``               — workspace shell (maps onto run_shell)
``str_replace_based_edit_tool`` — view / create / str_replace / insert

Declared as ordinary function tools for every provider. Anthropic additionally
gets the dated server-tool *shape* so Claude emits the trained names; we still
execute locally (computer-use is a client protocol).
"""

from __future__ import annotations

from pathlib import Path

from oaset.tools.base import EXEC, WRITE, Tool, ToolContext, ToolResult
from oaset.tools.fs import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from oaset.tools.shell import RunShellTool
from oaset.utils import expand_path, short_path


class ComputerTool(Tool):
    """Computer-use client tool, executed on this machine."""

    name = "computer"
    description = (
        "Use a mouse and keyboard to interact with a computer, and take screenshots. "
        "Coordinates are in screenshot pixels from the last screenshot. "
        "After screenshot / click / type / key, a new screenshot is returned. "
        "ALWAYS screenshot before clicking so coordinates match the current frame."
    )
    permission = EXEC
    required = ["action"]
    parameters = {
        "action": {
            "type": "string",
            "description": (
                "screenshot | mouse_move | left_click | right_click | middle_click | "
                "double_click | triple_click | left_click_drag | left_mouse_down | "
                "left_mouse_up | scroll | type | key | hold_key | wait | "
                "cursor_position | zoom"
            ),
        },
        "coordinate": {"type": "array",
                       "description": "[x, y] in screenshot pixels (from the last screenshot)"},
        "start_coordinate": {"type": "array",
                             "description": "Drag start [x, y] (left_click_drag)"},
        "text": {"type": "string",
                 "description": "Text to type, key chord (ctrl+c), or held modifier"},
        "scroll_direction": {"type": "string", "description": "up | down | left | right"},
        "scroll_amount": {"type": "integer", "description": "Wheel ticks (default 1)"},
        "duration": {"type": "number", "description": "Seconds for wait / hold_key"},
        "repeat": {"type": "integer", "description": "Times to press `key` (1–100)"},
        "key": {"type": "string", "description": "Modifier held during a click (legacy)"},
        "region": {"type": "array",
                   "description": "zoom crop [x0, y0, x1, y1] in screenshot pixels"},
    }

    def gate_summary(self, args, ctx):
        action = str(args.get("action") or "")
        coord = args.get("coordinate")
        extra = f" @ {coord}" if coord else ""
        return f"computer {action}{extra}  ⚠ DESKTOP INPUT"

    def permanent_allow(self, args, ctx) -> bool:
        return False  # never persist desktop input across sessions

    def in_fence(self, args, ctx) -> bool:
        return False  # the whole desktop is outside the workspace

    async def run(self, args, ctx):
        from oaset.computer.use import session_for

        session = session_for(ctx)
        result = await session.act(args)
        if result.is_error:
            return ToolResult(result.error, is_error=True)
        return ToolResult(result.output or "ok", image=result.png,
                          image_mime="image/png")


class BashTool(Tool):
    """`bash` client tool — same sandbox/gate as run_shell."""

    name = "bash"
    description = (
        "Run a shell command in the workspace. command is required unless restart=true "
        "(which just acknowledges a fresh session — oAset has no persistent bash tty)."
    )
    permission = EXEC
    required = []

    def danger_level(self, args, ctx) -> str:
        # the bash ALIAS must carry run_shell's danger classification: without
        # it, sudo / rm -rf ran silently under auto mode or a session-level
        # always granted for an ordinary command
        from oaset.tools.danger import classify_command

        return classify_command(str(args.get('command', '')))
    parameters = {
        "command": {"type": "string", "description": "Shell command line"},
        "restart": {"type": "boolean", "description": "Reset the bash session (no-op here)"},
    }

    def subject(self, args, ctx):
        return str(args.get("command", ""))

    def blocked_reason(self, args, ctx):
        return RunShellTool().blocked_reason(
            {"command": str(args.get("command") or "")}, ctx)

    def permanent_allow(self, args, ctx) -> bool:
        return RunShellTool().permanent_allow(
            {"command": str(args.get("command") or "")}, ctx)

    def gate_summary(self, args, ctx):
        return RunShellTool().gate_summary(
            {"command": str(args.get("command") or "")}, ctx)

    async def run(self, args, ctx):
        if args.get("restart"):
            return ToolResult("bash session restarted")
        command = args.get("command")
        if not command:
            return ToolResult("no command provided", is_error=True)
        return await RunShellTool().run({"command": str(command)}, ctx)


class TextEditorTool(Tool):
    """`str_replace_based_edit_tool` — view/create/str_replace/insert."""

    name = "str_replace_based_edit_tool"
    description = (
        "View, create, and edit files. Commands: view, create, str_replace, insert. "
        "path may be relative to the workspace."
    )
    permission = WRITE
    required = ["command", "path"]
    parameters = {
        "command": {"type": "string",
                    "description": "view | create | str_replace | insert"},
        "path": {"type": "string", "description": "File or directory path"},
        "file_text": {"type": "string", "description": "Full contents for create"},
        "old_str": {"type": "string", "description": "Exact text to find (str_replace)"},
        "new_str": {"type": "string", "description": "Replacement text (str_replace)"},
        "insert_line": {"type": "integer",
                        "description": "0-based line after which to insert"},
        "insert_text": {"type": "string", "description": "Text to insert"},
        "view_range": {"type": "array",
                       "description": "[start, end] 1-based inclusive; end=-1 means EOF"},
    }

    def subject(self, args, ctx):
        return str(args.get("path", ""))

    def permanent_allow(self, args, ctx) -> bool:
        return True

    def in_fence(self, args, ctx) -> bool:
        return WriteFileTool().in_fence({"path": args.get("path") or "."}, ctx)

    def fence_paths(self, args, ctx):
        return [expand_path(args.get("path") or ".", ctx.cwd)]

    def checkpoint_paths(self, args, ctx):
        if str(args.get("command") or "") in ("create", "str_replace", "insert"):
            return [expand_path(args["path"], ctx.cwd)]
        return []

    def gate_summary(self, args, ctx):
        cmd = str(args.get("command") or "")
        path = short_path(expand_path(args.get("path") or ".", ctx.cwd), ctx.cwd)
        return f"str_replace_based_edit_tool {cmd} {path}"

    async def run(self, args, ctx):
        command = str(args.get("command") or "").strip()
        raw_path = str(args.get("path") or "")
        if not command or not raw_path:
            return ToolResult("command and path are required", is_error=True)
        path = expand_path(raw_path, ctx.cwd)
        if command == "view":
            return await self._view(path, args, ctx)
        if command == "create":
            text = args.get("file_text")
            if text is None:
                return ToolResult("file_text is required for create", is_error=True)
            if path.exists():
                return ToolResult(f"File already exists at: {path}. Cannot overwrite with create.",
                                  is_error=True)
            return await WriteFileTool().run({"path": raw_path, "content": str(text)}, ctx)
        if command == "str_replace":
            old = args.get("old_str")
            if old is None:
                return ToolResult("old_str is required for str_replace", is_error=True)
            return await EditFileTool().run(
                {"path": raw_path, "old_text": str(old),
                 "new_text": str(args.get("new_str") or "")}, ctx)
        if command == "insert":
            return await self._insert(path, args, ctx)
        return ToolResult(
            f"Unrecognized command {command}. Allowed: view, create, str_replace, insert",
            is_error=True)

    async def _view(self, path: Path, args, ctx: ToolContext) -> ToolResult:
        if path.is_dir():
            if args.get("view_range"):
                return ToolResult("view_range is not allowed on a directory", is_error=True)
            return await ListDirTool().run({"path": str(path)}, ctx)
        view_range = args.get("view_range")
        offset, limit = 1, 2000
        if view_range is not None:
            if not isinstance(view_range, (list, tuple)) or len(view_range) != 2:
                return ToolResult("view_range must be [start, end]", is_error=True)
            start, end = int(view_range[0]), int(view_range[1])
            offset = max(1, start)
            limit = 2000 if end == -1 else max(1, end - offset + 1)
        return await ReadFileTool().run(
            {"path": str(path), "offset": offset, "limit": limit}, ctx)

    async def _insert(self, path: Path, args, ctx: ToolContext) -> ToolResult:
        if args.get("insert_line") is None or args.get("insert_text") is None:
            return ToolResult("insert_line and insert_text are required for insert",
                              is_error=True)
        if not path.is_file():
            return ToolResult(f"File not found: {path}", is_error=True)
        line_no = int(args["insert_line"])
        insert = str(args["insert_text"])
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        if line_no < 0 or line_no > len(lines):
            return ToolResult(
                f"insert_line {line_no} out of range [0, {len(lines)}]", is_error=True)
        if insert and not insert.endswith("\n"):
            insert += "\n"
        new_lines = lines[:line_no] + [insert] + lines[line_no:]
        path.write_text("".join(new_lines), encoding="utf-8")
        return ToolResult(f"Inserted at line {line_no} in {short_path(path, ctx.cwd)}")


def computer_use_tools() -> list[Tool]:
    return [ComputerTool(), BashTool(), TextEditorTool()]
