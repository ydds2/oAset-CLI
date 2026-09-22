"""REAL browser driver e2e (CDP over Edge/Chrome headless) — any host with
the browser installed; skipped when none is discoverable.

All actions flow through GatedBrowserPage (approval gate + NetworkPolicy)
exactly as the shipped product path would.
"""

from __future__ import annotations

import contextlib
import http.server
import pathlib
import threading

import pytest

from oaset.computer import BrowserError, GatedBrowserPage
from oaset.computer.base import Screenshot
from oaset.computer.cdp import CdpBrowserPage, _find_browser_executable
from oaset.network import NetworkPolicy


def _browser_available() -> bool:
    try:
        _find_browser_executable()
        return True
    except BrowserError:
        return False


pytestmark = pytest.mark.skipif(
    not _browser_available(), reason="no Edge/Chrome browser available")


class AllowGate:
    async def request(self, tool_name, level, summary, preview=None):
        return "always"


class DenyGate:
    async def request(self, tool_name, level, summary, preview=None):
        return "deny"


PAGE_HTML = """<!doctype html>
<html><head><title>oaset cdp test</title></head>
<body>
  <input id="q" name="q" placeholder="search">
  <input id="f" type="file">
  <button id="go" onclick="document.getElementById('out').textContent='clicked ' + document.getElementById('q').value;">Go</button>
  <div id="out"></div>
</body></html>"""


@pytest.fixture()
async def page(tmp_path):
    fixture = tmp_path / "page.html"
    fixture.write_text(PAGE_HTML, encoding="utf-8")
    try:
        browser = await CdpBrowserPage.launch(url=fixture.as_uri())
    except BrowserError as exc:
        # runners may have a browser binary but no working launch environment
        pytest.skip(f"browser launch unavailable: {exc}")
    gated = GatedBrowserPage(browser, AllowGate(), NetworkPolicy("full"))
    yield gated, browser
    await browser.close()


async def test_real_observe_lists_actionable_elements(page):
    gated, _ = page
    snap = await gated.observe()
    assert snap["title"] == "oaset cdp test"
    assert snap["url"].startswith("file://")
    tags = {e.tag for e in snap["elements"]}
    assert {"input", "button"} <= tags
    button = next(e for e in snap["elements"] if e.tag == "button")
    assert button.text == "Go"
    # viewport CSS pixels: both coordinates and sizes are positive integers
    assert button.rect["w"] > 0 and button.rect["h"] > 0
    assert isinstance(button.rect["x"], int)


async def test_real_find_by_selector(page):
    gated, _ = page
    element = await gated.find("#go")
    assert element is not None
    assert element.tag == "button" and element.text == "Go"
    assert element.handle == "css:#go"
    assert await gated.find("#no-such-thing") is None


async def test_real_type_text_and_click_reflect_on_page(page):
    gated, browser = page
    assert await gated.type_text("css:#q", "hello cdp") is True
    assert await gated.click("css:#go") is True
    # the click handler wrote into #out — read it back through the driver
    assert await browser._eval(
        "document.getElementById('out').textContent") == "clicked hello cdp"


async def test_real_screenshot_png_with_metrics(page):
    gated, _ = page
    shot = await gated.screenshot()
    assert isinstance(shot, Screenshot)
    assert shot.mime == "image/png" and shot.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert shot.width > 0 and shot.height > 0


async def test_real_gate_denial_blocks_click_and_leaves_page(page):
    gated = GatedBrowserPage(page[1], DenyGate(), NetworkPolicy("full"))
    with pytest.raises(BrowserError) as excinfo:
        await gated.click("css:#go")
    assert excinfo.value.code == "permission_denied"
    # page untouched: the click handler never ran
    assert await page[1]._eval("document.getElementById('out').textContent") == ""


async def test_real_local_only_blocks_all_actions(page):
    gated = GatedBrowserPage(page[1], AllowGate(), NetworkPolicy("local_only"))
    actions = [gated.observe, gated.screenshot,
               lambda: gated.click("css:#go"),
               lambda: gated.type_text("css:#q", "x")]
    for action in actions:
        with pytest.raises(BrowserError) as excinfo:
            await action()
        assert excinfo.value.code == "network_blocked"
    assert await page[1]._eval("document.getElementById('out').textContent") == ""


# --------------------------------------------- headed mode + download/upload

DOWNLOAD_PAYLOAD = b"oaset download payload\n"


class _AttachmentHandler(http.server.BaseHTTPRequestHandler):
    """Serves one file with Content-Disposition: attachment."""

    def do_GET(self):  # noqa: N802 (stdlib handler API)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         'attachment; filename="oaset-file.txt"')
        self.send_header("Content-Length", str(len(DOWNLOAD_PAYLOAD)))
        self.end_headers()
        self.wfile.write(DOWNLOAD_PAYLOAD)

    def log_message(self, *args):  # silence the test console
        pass


@contextlib.contextmanager
def _attachment_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AttachmentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/file"
    finally:
        server.shutdown()


async def test_real_headed_launch_runs_a_visible_browser(tmp_path):
    """headless=False launches a REAL browser window (a human can watch); the
    CDP plumbing is identical. Runners without a desktop skip."""
    fixture = tmp_path / "headed.html"
    fixture.write_text(PAGE_HTML, encoding="utf-8")
    try:
        browser = await CdpBrowserPage.launch(url=fixture.as_uri(),
                                              headless=False)
    except BrowserError as exc:
        pytest.skip(f"headed launch unavailable in this environment: {exc}")
    try:
        snap = await browser.observe()
        assert snap["title"] == "oaset cdp test"
    finally:
        await browser.close()


async def test_real_download_gated_and_verified_on_disk(page):
    gated, _ = page
    with _attachment_server() as url:
        import tempfile as _tempfile

        dest = _tempfile.mkdtemp(prefix="oaset-dl-")
        result = await gated.download(url, dest)
        assert result.suggested_filename == "oaset-file.txt"
        assert result.size_bytes == len(DOWNLOAD_PAYLOAD)
        assert result.path.read_bytes() == DOWNLOAD_PAYLOAD
        # the deny policy is restored afterwards: navigating to the attachment
        # URL directly must NOT land a second file
        before = {p.name for p in result.path.parent.iterdir()}
        import asyncio as _asyncio

        await page[1].navigate(url)
        await _asyncio.sleep(1.5)
        after = {p.name for p in result.path.parent.iterdir()}
        # Chrome may leave an in-progress `.crdownload` temp even though the
        # CDP handler cancelled the download (CI timing) — the contract is
        # that no COMPLETED file lands; the transient temp is browser noise
        # the user cannot use.
        after_final = {n for n in after if not n.endswith(".crdownload")}
        assert after_final == before, "site-initiated download must stay denied"


async def test_real_download_denied_by_gate_writes_nothing(page):
    gated = GatedBrowserPage(page[1], DenyGate(), NetworkPolicy("full"))
    import tempfile as _tempfile

    dest = _tempfile.mkdtemp(prefix="oaset-dl-")
    with _attachment_server() as url:
        with pytest.raises(BrowserError) as excinfo:
            await gated.download(url, dest)
    assert excinfo.value.code == "permission_denied"
    assert list(pathlib.Path(dest).iterdir()) == []


async def test_real_download_blocked_under_local_only(page):
    gated = GatedBrowserPage(page[1], AllowGate(), NetworkPolicy("local_only"))
    import tempfile as _tempfile

    dest = _tempfile.mkdtemp(prefix="oaset-dl-")
    with _attachment_server() as url:
        with pytest.raises(BrowserError) as excinfo:
            await gated.download(url, dest)
    assert excinfo.value.code == "network_blocked"


async def test_real_download_missing_dest_rejected_before_gate(page):
    gated = GatedBrowserPage(page[1], DenyGate(), NetworkPolicy("full"))
    with _attachment_server() as url:
        with pytest.raises(BrowserError) as excinfo:
            await gated.download(url, "Z:/definitely/not/here")
    # dest validation precedes the approval round-trip
    assert excinfo.value.code == "download_dest_missing"


async def test_real_upload_attaches_file_via_dom(page, tmp_path):
    gated, browser = page
    target = tmp_path / "upload-me.txt"
    target.write_text("upload payload", encoding="utf-8")
    assert await gated.upload("css:#f", [target]) is True
    count = await browser._eval("document.getElementById('f').files.length")
    assert count == 1
    name = await browser._eval("document.getElementById('f').files[0].name")
    assert name == "upload-me.txt"


async def test_real_upload_missing_file_rejected_before_gate(page, tmp_path):
    gated = GatedBrowserPage(page[1], DenyGate(), NetworkPolicy("full"))
    with pytest.raises(BrowserError) as excinfo:
        await gated.upload("css:#f", [tmp_path / "ghost.bin"])
    assert excinfo.value.code == "upload_file_missing"


async def test_real_upload_denied_by_gate(page, tmp_path):
    gated = GatedBrowserPage(page[1], DenyGate(), NetworkPolicy("full"))
    target = tmp_path / "nope.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(BrowserError) as excinfo:
        await gated.upload("css:#f", [target])
    assert excinfo.value.code == "permission_denied"
    count = await page[1]._eval("document.getElementById('f').files.length")
    assert count == 0


async def test_real_upload_requires_push_mode(page, tmp_path):
    gated = GatedBrowserPage(page[1], AllowGate(), NetworkPolicy("pull_only"))
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(BrowserError) as excinfo:
        await gated.upload("css:#f", [target])
    assert excinfo.value.code == "network_blocked"
