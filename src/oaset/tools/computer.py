"""Computer-use agent tools — the product wiring for computer/cdp.py,
computer/desktop.py and computer/vision.py.

These tools let the AGENT drive a real browser (CDP over Edge/Chrome) and
the real Windows desktop (UIA) through the same Gated* wrappers the TUI
uses: every action passes the registry's PermissionGate, and browser
network-sensitive actions honor ToolContext.network_mode.

Backend lifecycle:
  - the browser is launched lazily on first use and cached in
    session_state["browser_page"]; `browser_close` shuts it down;
  - the desktop backend is the registered UIA backend (Windows); tests may
    inject a fake via session_state["desktop_backend"].
"""

from __future__ import annotations

import sys
from typing import Any

from oaset.computer import GatedBrowserPage, GatedDesktopPage, UiaDesktopPage
from oaset.computer.base import BrowserError
from oaset.computer.cdp import CdpBrowserPage
from oaset.computer.desktop import DesktopError
from oaset.tools.base import Tool, ToolContext, ToolResult


def _err(exc: Exception) -> ToolResult:
    """Convert structured browser/desktop failures into error results."""
    code = getattr(exc, "code", "")
    return ToolResult(f"[{code}] {exc}" if code else str(exc), is_error=True)


def _gate(ctx: ToolContext):
    gate = getattr(ctx, "gate", None)
    if gate is None:  # registries that never bound a gate default to deny
        class _DenyAll:
            async def request(self, tool_name, level, summary, preview=None):
                return "deny"
        return _DenyAll()
    return gate


async def _browser(ctx: ToolContext) -> GatedBrowserPage:
    page = ctx.session_state.get("browser_page")
    if page is None:
        page = await CdpBrowserPage.launch()
        ctx.session_state["browser_page"] = page
    from oaset.network import NetworkPolicy

    return GatedBrowserPage(page, _gate(ctx), NetworkPolicy(ctx.network_mode))


def _desktop(ctx: ToolContext) -> GatedDesktopPage:
    backend = ctx.session_state.get("desktop_backend")
    if backend is None:
        from oaset.computer.desktop import _current_backend

        backend = _current_backend()
    return GatedDesktopPage(UiaDesktopPage(backend), _gate(ctx))


def _render_element(el) -> str:
    parts = [f"{el.handle} <{el.tag or el.control_type}>"]
    if el.text:
        parts.append(repr(el.text[:60]))
    rect = el.rect or {}
    if rect:
        parts.append(f"rect=({rect.get('x', 0)},{rect.get('y', 0)} "
                     f"{rect.get('w', 0)}x{rect.get('h', 0)})")
    return " ".join(parts)


# ------------------------------------------------------------------- browser


class BrowserOpenTool(Tool):
    network = True
    name = "browser_open"
    description = (
        "Launch or reuse a real Edge/Chrome browser (CDP session) and navigate "
        "to url. headless=false opens a visible window the user can watch. "
        "The browser is reused for later browser_* tools."
    )
    permission = "exec"
    required = ["url"]
    parameters = {
        "url": {"type": "string", "description": "http(s) or file:// URL to open"},
        "headless": {"type": "boolean", "description":
                     "true (default) = invisible; false = real browser window",
                     "optional": True},
    }

    async def run(self, args, ctx):
        page = ctx.session_state.get("browser_page")
        if page is None:
            headless = bool(args.get("headless", True))
            page = await CdpBrowserPage.launch(
                url=str(args["url"]), headless=headless)
            ctx.session_state["browser_page"] = page
        gated = await _browser(ctx)
        await gated.page.navigate(str(args["url"]))
        try:
            snap = await gated.observe()
        except (BrowserError, DesktopError) as exc:
            return _err(exc)
        return ToolResult(f"opened {snap['url']} — {snap['title']}")


class BrowserObserveTool(Tool):
    network = True
    name = "browser_observe"
    description = (
        "List interactive elements (links, buttons, inputs) of the current "
        "browser page with their oaset handles for browser_click/browser_type."
    )
    permission = "read"
    parameters: dict[str, Any] = {}

    async def run(self, args, ctx):
        gated = await _browser(ctx)
        try:
            snap = await gated.observe()
        except (BrowserError, DesktopError) as exc:
            return _err(exc)
        lines = [f"{snap['url']} — {snap['title']}"]
        for el in snap["elements"]:
            lines.append(_render_element(el))
        return ToolResult("\n".join(lines) or "(no interactive elements)")


class BrowserClickTool(Tool):
    network = True
    name = "browser_click"
    description = (
        "Click a page element by handle (from browser_observe) or css selector "
        "prefixed with 'css:'. May navigate or submit data."
    )
    permission = "exec"
    required = ["handle"]
    parameters = {
        "handle": {"type": "string",
                   "description": "Element handle: 'oaset:<n>' or 'css:<selector>'"},
    }

    async def run(self, args, ctx):
        gated = await _browser(ctx)
        try:
            clicked = await gated.click(str(args["handle"]))
        except (BrowserError, DesktopError) as exc:
            return _err(exc)
        if not clicked:
            return ToolResult(f"stale or unknown handle: {args['handle']}", is_error=True)
        return ToolResult("clicked")


class BrowserTypeTool(Tool):
    network = True
    name = "browser_type"
    description = (
        "Type text into a page input by handle or 'css:<selector>'. Replaces "
        "the current value and fires input events."
    )
    permission = "exec"
    required = ["handle", "text"]
    parameters = {
        "handle": {"type": "string", "description": "Element handle"},
        "text": {"type": "string", "description": "Text to type"},
    }

    async def run(self, args, ctx):
        gated = await _browser(ctx)
        try:
            typed = await gated.type_text(str(args["handle"]), str(args["text"]))
        except (BrowserError, DesktopError) as exc:
            return _err(exc)
        if not typed:
            return ToolResult(f"stale or unknown handle: {args['handle']}", is_error=True)
        return ToolResult("typed")


class BrowserScreenshotTool(Tool):
    network = True
    name = "browser_screenshot"
    description = "Capture a PNG screenshot of the current browser page."
    permission = "read"
    parameters: dict[str, Any] = {}

    async def run(self, args, ctx):
        gated = await _browser(ctx)
        try:
            shot = await gated.screenshot()
        except (BrowserError, DesktopError) as exc:
            return _err(exc)
        path = ctx.session_state.get("last_screenshot_path")
        return ToolResult(
            f"{shot.summary()} (bytes available in session; last path: {path})")


class BrowserCloseTool(Tool):
    network = True
    name = "browser_close"
    description = "Close the browser session started by browser_open."
    permission = "read"
    parameters: dict[str, Any] = {}

    async def run(self, args, ctx):
        page = ctx.session_state.pop("browser_page", None)
        if page is None:
            return ToolResult("no browser session")
        await page.close()
        return ToolResult("browser closed")


class BrowserDownloadTool(Tool):
    network = True
    name = "browser_download"
    description = (
        "Download a URL's file through the browser into a directory. dest is "
        "optional: it must be an existing directory inside the workspace; "
        "otherwise ~/.oaset/downloads is used. The browser denies every other "
        "download."
    )
    permission = "exec"
    required = ["url"]
    parameters = {
        "url": {"type": "string", "description": "http(s) or file:// URL of the file"},
        "dest": {"type": "string", "description":
                 "Optional existing destination directory inside the workspace",
                 "optional": True},
    }

    async def run(self, args, ctx):
        from pathlib import Path

        dest_arg = str(args["dest"]) if args.get("dest") else ""
        if dest_arg:
            dest = Path(dest_arg)
            if not dest.is_absolute():
                dest = Path(ctx.cwd) / dest
            workspace = Path(ctx.cwd).resolve()
            if not dest.resolve().is_relative_to(workspace):
                return ToolResult(
                    "[download_dest_outside_workspace] dest must stay inside "
                    f"the workspace ({workspace})", is_error=True)
        else:
            from oaset.utils import oaset_home

            dest = oaset_home() / "downloads"
        dest.mkdir(parents=True, exist_ok=True)
        gated = await _browser(ctx)
        try:
            result = await gated.download(str(args["url"]), dest)
        except (BrowserError, DesktopError) as exc:
            return _err(exc)
        return ToolResult(
            f"downloaded {result.suggested_filename or result.path.name} "
            f"({result.size_bytes} bytes) -> {result.path}")


class BrowserUploadTool(Tool):
    network = True
    name = "browser_upload"
    description = (
        "Attach local file(s) to a page file input (handle from browser_observe "
        "or 'css:selector'). Paths resolve inside the workspace and must exist; "
        "the site receives the file contents."
    )
    permission = "exec"
    required = ["handle", "files"]
    parameters = {
        "handle": {"type": "string", "description": "File input element handle"},
        "files": {"type": "array", "description":
                  "Workspace-relative or absolute file paths (max 10)"},
    }

    @staticmethod
    def _resolved(args, ctx) -> list:
        from pathlib import Path

        files = []
        for item in args.get("files") or []:
            p = Path(str(item))
            files.append(p if p.is_absolute() else Path(ctx.cwd) / p)
        return files

    def in_fence(self, args, ctx) -> bool:
        """Uploading hands file CONTENTS to a website — out of the workspace is
        an explicit decision, never a session-wide 'always' (the gate then asks
        every time and cannot persist the grant)."""
        from oaset.tools.fs import _within

        return all(_within(p, ctx.cwd) for p in self._resolved(args, ctx))

    def gate_summary(self, args, ctx) -> str:
        """Full paths, with the destination named: the basename alone hid that
        an upload was reaching outside the workspace."""
        from oaset.tools.fs import _within
        from oaset.utils import short_path

        shown = ", ".join(
            short_path(p, ctx.cwd) + ("" if _within(p, ctx.cwd) else " (outside)")
            for p in self._resolved(args, ctx))
        return f"browser_upload {shown or '(no files)'} -> {args.get('handle', '?')}"

    async def run(self, args, ctx):
        files = self._resolved(args, ctx)
        gated = await _browser(ctx)
        try:
            attached = await gated.upload(str(args["handle"]), files)
        except (BrowserError, DesktopError) as exc:
            return _err(exc)
        return ToolResult(
            "attached " + ", ".join(p.name for p in files) if attached
            else f"stale or unknown handle: {args['handle']}")


# -------------------------------------------------------------------- desktop


class DesktopWindowsTool(Tool):
    name = "desktop_windows"
    description = (
        "List visible desktop windows (title, pid, rect, DPI). Windows-only; "
        "requires the UIA backend."
    )
    permission = "read"
    parameters: dict[str, Any] = {}

    async def run(self, args, ctx):
        gated = _desktop(ctx)
        try:
            windows = await gated.list_windows()
        except DesktopError as exc:
            return _err(exc)
        lines = []
        for w in windows[:20]:
            r = w.rect or {}
            lines.append(f"{w.handle} {w.title[:50]!r} pid={w.pid} "
                         f"({r.get('x', 0)},{r.get('y', 0)} "
                         f"{r.get('w', 0)}x{r.get('h', 0)} @{r.get('scale_factor', 1)}x)"
                         + (" [foreground]" if w.foreground else ""))
        return ToolResult("\n".join(lines) or "(no windows)")


class DesktopFindTool(Tool):
    name = "desktop_find"
    description = (
        "Find a desktop UI element by Name inside a window handle "
        "(from desktop_windows). Returns its handle for desktop_click/type."
    )
    permission = "read"
    required = ["window", "name"]
    parameters = {
        "window": {"type": "string", "description": "Window handle"},
        "name": {"type": "string", "description": "Element Name to search for"},
    }

    async def run(self, args, ctx):
        gated = _desktop(ctx)
        try:
            el = await gated.find(str(args["window"]), str(args["name"]))
        except DesktopError as exc:
            return _err(exc)
        if el is None:
            return ToolResult("not found", is_error=True)
        return ToolResult(_render_element(el))


class DesktopClickTool(Tool):
    name = "desktop_click"
    description = (
        "Click a desktop UI element by handle (InvokePattern when available). "
        "Every click is approval-gated."
    )
    permission = "exec"
    required = ["handle"]
    parameters = {
        "handle": {"type": "string", "description": "Element handle from desktop_find"},
    }

    async def run(self, args, ctx):
        gated = _desktop(ctx)
        try:
            clicked = await gated.click(str(args["handle"]))
        except DesktopError as exc:
            return _err(exc)
        return ToolResult("clicked" if clicked else "stale handle",
                          is_error=not clicked)


class DesktopTypeTool(Tool):
    name = "desktop_type"
    description = (
        "Replace the value of a desktop Edit control by handle (ValuePattern). "
        "Approval-gated."
    )
    permission = "exec"
    required = ["handle", "text"]
    parameters = {
        "handle": {"type": "string", "description": "Element handle"},
        "text": {"type": "string", "description": "Text to set"},
    }

    async def run(self, args, ctx):
        gated = _desktop(ctx)
        try:
            typed = await gated.type_text(str(args["handle"]), str(args["text"]))
        except DesktopError as exc:
            return _err(exc)
        return ToolResult("typed" if typed else "stale handle",
                          is_error=not typed)


class DesktopScreenshotTool(Tool):
    name = "desktop_screenshot"
    description = (
        "Capture a BMP screenshot of a window (or the whole desktop when no "
        "window is given). Approval-gated; returns size and metadata."
    )
    permission = "read"
    required = []
    parameters = {
        "window": {"type": "string", "description": "Optional window handle"},
    }

    async def run(self, args, ctx):
        gated = _desktop(ctx)
        try:
            shot = await gated.screenshot(args.get("window"))
        except DesktopError as exc:
            return _err(exc)
        meta = shot.metadata()
        return ToolResult(
            f"{shot.summary()} space={meta['coordinate_space']} "
            f"display={meta['display_id']} captured_at={meta['captured_at']}")


def computer_tools() -> list[Tool]:
    """Browser tools everywhere; desktop tools only where UIA exists."""
    tools: list[Tool] = [
        BrowserOpenTool(),
        BrowserObserveTool(),
        BrowserClickTool(),
        BrowserTypeTool(),
        BrowserDownloadTool(),
        BrowserUploadTool(),
        BrowserScreenshotTool(),
        BrowserCloseTool(),
    ]
    if sys.platform == "win32":
        tools.extend([
            DesktopWindowsTool(),
            DesktopFindTool(),
            DesktopClickTool(),
            DesktopTypeTool(),
            DesktopScreenshotTool(),
        ])
    return tools
