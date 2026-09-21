"""Computer-use agent tools: gating, handles, lifecycle — with injected
fake backends so no real browser/desktop is needed for the logic tests."""

from __future__ import annotations

from oaset.computer.base import PageElement, Screenshot
from oaset.computer.desktop import (
    DesktopElement,
    DesktopScreenshot,
    DesktopWindow,
    _rect,
)
from oaset.tools.base import ToolContext
from oaset.tools.computer import (
    BrowserClickTool,
    BrowserCloseTool,
    BrowserDownloadTool,
    BrowserObserveTool,
    BrowserOpenTool,
    BrowserUploadTool,
    DesktopClickTool,
    DesktopWindowsTool,
)


class AllowGate:
    async def request(self, tool_name, level, summary, preview=None):
        return "always"


class DenyGate:
    async def request(self, tool_name, level, summary, preview=None):
        return "deny"


class FakeCdpPage:
    def __init__(self):
        self.url = "https://x.test/"
        self.navigated = []
        self.clicked = []
        self.typed = []
        self.downloads = []
        self.uploads = []

    async def download(self, url, dest_dir, timeout=60.0):
        from oaset.computer.cdp import DownloadResult

        self.downloads.append((url, dest_dir))
        out = dest_dir / "oaset-file.txt"
        out.write_bytes(b"payload")
        return DownloadResult(path=out, suggested_filename="oaset-file.txt",
                              size_bytes=7, url=url)

    async def upload(self, handle, files):
        self.uploads.append((handle, files))
        return True

    async def navigate(self, url):
        self.url = url
        self.navigated.append(url)

    async def observe(self):
        el = PageElement(handle="oaset:0", tag="button", text="Go",
                         rect={"x": 1, "y": 2, "w": 30, "h": 10})
        return {"url": self.url, "title": "T", "elements": [el]}

    async def click(self, handle):
        self.clicked.append(handle)
        return True

    async def type_text(self, handle, text):
        self.typed.append((handle, text))
        return True

    async def screenshot(self):
        return Screenshot(mime="image/png", data=b"\x89PNG", width=100, height=40)

    async def close(self):
        self.closed = True


class FakeUiaBackend:
    def __init__(self):
        self.windows = [DesktopWindow(handle="w1", title="App", pid=1,
                                      rect=_rect(0, 0, 800, 600, scale=1.0, display=0),
                                      foreground=True)]
        self.elements = {"b1": DesktopElement(handle="b1", window_handle="w1",
                                              control_type="Button", name="OK",
                                              rect=_rect(10, 10, 60, 20, scale=1.0, display=0))}
        self.clicked = []
        self.typed = {}

    def list_windows(self):
        return self.windows

    def find_element(self, window_handle, selector):
        el = self.elements.get(selector)
        return el if el and el.window_handle == window_handle else None

    def elements(self, window_handle):
        return [e for e in self.elements.values() if e.window_handle == window_handle]

    def click_element(self, handle):
        if handle not in self.elements:
            return False
        self.clicked.append(handle)
        return True

    def type_text(self, handle, text):
        if handle not in self.elements:
            return False
        self.typed[handle] = text
        return True

    def capture(self, window_handle):
        return DesktopScreenshot(mime="image/bmp", data=b"BMfake",
                                 width=800, height=600, scale_factor=1.0,
                                 display_id=0, window_handle=window_handle)


def _ctx(gate=None, network_mode="full"):
    ctx = ToolContext(cwd=None, mode="auto", network_mode=network_mode, gate=gate)
    ctx.session_state["browser_page"] = FakeCdpPage()
    ctx.session_state["desktop_backend"] = FakeUiaBackend()
    return ctx


async def test_browser_observe_and_click_through_gate():
    ctx = _ctx(AllowGate())
    observe = await BrowserObserveTool().run({}, ctx)
    assert "oaset:0" in observe.output and "Go" in observe.output
    click = await BrowserClickTool().run({"handle": "oaset:0"}, ctx)
    assert not click.is_error


async def test_browser_click_denied_by_gate_is_error():
    ctx = _ctx(DenyGate())
    result = await BrowserClickTool().run({"handle": "oaset:0"}, ctx)
    assert result.is_error and "denied" in result.output


async def test_browser_policy_local_only_blocks_but_full_allows():
    ctx = _ctx(AllowGate(), network_mode="local_only")
    result = await BrowserClickTool().run({"handle": "oaset:0"}, ctx)
    assert result.is_error and "network" in result.output.lower() or "full" in result.output
    ctx2 = _ctx(AllowGate(), network_mode="full")
    ok = await BrowserClickTool().run({"handle": "oaset:0"}, ctx2)
    assert not ok.is_error


async def test_browser_open_navigates_existing_session():
    ctx = _ctx(AllowGate())
    result = await BrowserOpenTool().run({"url": "https://new.test/"}, ctx)
    assert "new.test" in result.output


async def test_browser_close_removes_session():
    ctx = _ctx(AllowGate())
    page = ctx.session_state["browser_page"]
    result = await BrowserCloseTool().run({}, ctx)
    assert "closed" in result.output
    assert "browser_page" not in ctx.session_state
    assert page.closed is True


async def test_desktop_windows_and_click_use_injected_backend():
    ctx = _ctx(AllowGate())
    windows = await DesktopWindowsTool().run({}, ctx)
    assert "App" in windows.output and "foreground" in windows.output
    click = await DesktopClickTool().run({"handle": "b1"}, ctx)
    assert not click.is_error
    backend = ctx.session_state["desktop_backend"]
    assert backend.clicked == ["b1"]


async def test_desktop_click_denied_keeps_backend_untouched():
    ctx = _ctx(DenyGate())
    result = await DesktopClickTool().run({"handle": "b1"}, ctx)
    assert result.is_error and "denied" in result.output
    backend = ctx.session_state["desktop_backend"]
    assert backend.clicked == []


async def test_missing_gate_defaults_to_deny():
    ctx = _ctx(None)  # no gate bound: must refuse, not act
    result = await DesktopClickTool().run({"handle": "b1"}, ctx)
    assert result.is_error and "denied" in result.output


# ------------------------------------------------ browser download/upload tools


async def test_browser_download_tool_uses_workspace_dest(tmp_path):
    ctx = _ctx(AllowGate())
    ctx.cwd = tmp_path
    dest = tmp_path / "out"
    dest.mkdir()
    result = await BrowserDownloadTool().run(
        {"url": "https://cdn.test/f.txt", "dest": str(dest)}, ctx)
    assert not result.is_error, result.output
    url, dest_dir = ctx.session_state["browser_page"].downloads[-1]
    assert url == "https://cdn.test/f.txt"
    assert dest_dir == dest.resolve()


async def test_browser_download_tool_rejects_dest_outside_workspace(tmp_path):
    ctx = _ctx(AllowGate())
    ctx.cwd = tmp_path
    outside = tmp_path.parent / "not-workspace"
    outside.mkdir(exist_ok=True)
    result = await BrowserDownloadTool().run(
        {"url": "https://cdn.test/f.txt", "dest": str(outside)}, ctx)
    assert result.is_error
    assert "download_dest_outside_workspace" in result.output
    assert ctx.session_state["browser_page"].downloads == []


async def test_browser_download_tool_default_dest_is_oaset_home(monkeypatch, tmp_path):
    monkeypatch.setenv("OASET_HOME", str(tmp_path / "home"))
    ctx = _ctx(AllowGate())
    ctx.cwd = tmp_path
    result = await BrowserDownloadTool().run({"url": "https://cdn.test/f.txt"}, ctx)
    assert not result.is_error, result.output
    url, dest_dir = ctx.session_state["browser_page"].downloads[-1]
    assert dest_dir == tmp_path / "home" / "downloads"
    assert dest_dir.is_dir()  # created on demand


async def test_browser_download_tool_denied_by_gate_is_error():
    ctx = _ctx(DenyGate())
    result = await BrowserDownloadTool().run({"url": "https://cdn.test/f.txt"}, ctx)
    assert result.is_error and "denied" in result.output


async def test_browser_upload_tool_resolves_relative_and_reports(tmp_path):
    ctx = _ctx(AllowGate())
    ctx.cwd = tmp_path
    (tmp_path / "report.txt").write_text("data", encoding="utf-8")
    result = await BrowserUploadTool().run(
        {"handle": "css:#f", "files": ["report.txt"]}, ctx)
    assert not result.is_error, result.output
    assert "report.txt" in result.output
    handle, files = ctx.session_state["browser_page"].uploads[-1]
    assert handle == "css:#f"
    assert files == [tmp_path / "report.txt"]


async def test_browser_upload_tool_missing_file_errors_before_gate(tmp_path):
    ctx = _ctx(DenyGate())  # even a deny gate must not be reached first
    ctx.cwd = tmp_path
    result = await BrowserUploadTool().run(
        {"handle": "css:#f", "files": ["no-such.bin"]}, ctx)
    assert result.is_error and "upload_file_missing" in result.output


async def test_browser_upload_tool_denied_by_gate_is_error(tmp_path):
    ctx = _ctx(DenyGate())
    ctx.cwd = tmp_path
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    result = await BrowserUploadTool().run(
        {"handle": "css:#f", "files": ["a.txt"]}, ctx)
    assert result.is_error and "denied" in result.output


async def test_browser_upload_tool_local_only_is_network_blocked(tmp_path):
    ctx = _ctx(AllowGate(), network_mode="local_only")
    ctx.cwd = tmp_path
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    result = await BrowserUploadTool().run(
        {"handle": "css:#f", "files": ["a.txt"]}, ctx)
    assert result.is_error and "network_blocked" in result.output


# --------------------------------------------- upload fence (Batch D/E hardening)


def test_browser_upload_is_out_of_fence_outside_the_workspace(tmp_path):
    """Attaching a file hands its contents to a website: anything outside the
    workspace must be OUT of fence so the registry gate asks every time and
    cannot persist an 'always' for it."""
    from oaset.tools.computer import BrowserUploadTool

    ctx = _ctx(AllowGate())
    ctx.cwd = tmp_path
    outside = tmp_path.parent / "elsewhere" / "id_rsa"
    outside.parent.mkdir(exist_ok=True)
    outside.write_text("secret", encoding="utf-8")
    tool = BrowserUploadTool()

    assert tool.in_fence({"files": ["inside.txt"]}, ctx) is True
    assert tool.in_fence({"files": [str(outside)]}, ctx) is False
    # mixed: one outside path is enough to lose the fence
    assert tool.in_fence({"files": ["inside.txt", str(outside)]}, ctx) is False
    # `..` traversal resolves before the check, so it cannot buy the fence back
    assert tool.in_fence({"files": [str(tmp_path / ".." / "escape.txt")]}, ctx) is False


def test_browser_upload_summary_names_the_full_path_and_the_destination(tmp_path):
    """The old summary printed basenames, so the approval prompt could not show
    that an upload reached outside the workspace."""
    from oaset.tools.computer import BrowserUploadTool

    ctx = _ctx(AllowGate())
    ctx.cwd = tmp_path
    outside = tmp_path.parent / "elsewhere" / "id_rsa"
    outside.parent.mkdir(exist_ok=True)
    summary = BrowserUploadTool().gate_summary(
        {"handle": "css:#f", "files": [str(outside)]}, ctx)
    assert "id_rsa" in summary and "outside" in summary
    assert "css:#f" in summary
