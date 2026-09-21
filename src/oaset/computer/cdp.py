"""Real browser driver over the Chrome DevTools Protocol (CDP).

Connects to Microsoft Edge / Google Chrome launched with
--remote-debugging-port and drives the page over a WebSocket — no
third-party browser library (websockets + httpx only, both already deps).

Implements the BrowserPage protocol from computer/base.py, so the
GatedBrowserPage approval/policy wrapper applies unchanged:

- observe: annotates actionable elements with data-oaset-h attributes and
  returns them (handles "oaset:<n>" -> stable while the page is unchanged)
- find: handles "css:<selector>" resolved live on every action
- click: element.click() via Runtime.evaluate
- type_text: native value setter + input event (works with React/Vue), with
  a contenteditable fallback
- screenshot: Page.captureScreenshot → PNG bytes + layout viewport metrics
- download: site-initiated downloads are DENIED by default; an explicit,
  caller-provided destination directory enables exactly one download, which
  is awaited via Browser.downloadProgress events and verified on disk
- upload: DOM.setFileInputFiles into a page file input (no JS can do this —
  the browser enforces it), gated by the caller
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import tempfile
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from oaset.computer.base import BrowserError, PageElement, Screenshot
from oaset.i18n import t

MAX_UPLOAD_FILES = 10

OBSERVE_JS = r"""
(function () {
  var els = Array.prototype.slice.call(
    document.querySelectorAll('a,button,input,textarea,select,[role="button"],[onclick]'));
  var out = [];
  els.forEach(function (el, i) {
    el.setAttribute('data-oaset-h', String(i));
    var r = el.getBoundingClientRect();
    out.push({
      i: i,
      tag: el.tagName.toLowerCase(),
      text: ((el.innerText || el.value || el.getAttribute('aria-label') || '') + '').slice(0, 80),
      id: el.id || '',
      name: el.getAttribute('name') || '',
      rect: {x: Math.round(r.x), y: Math.round(r.y),
             w: Math.round(r.width), h: Math.round(r.height)}
    });
  });
  return {url: location.href, title: document.title, elements: out};
})()
"""

FIND_JS = """
(function (el) {
  if (!el) return null;
  var r = el.getBoundingClientRect();
  return {tag: el.tagName.toLowerCase(),
          text: ((el.innerText || el.value || '') + '').slice(0, 80),
          rect: {x: Math.round(r.x), y: Math.round(r.y),
                 w: Math.round(r.width), h: Math.round(r.height)}};
})(__ELEMENT__)
"""

CLICK_JS = """
(function (el) {
  if (!el) return false;
  el.click();
  return true;
})(__ELEMENT__)
"""

TYPE_JS = """
(function (el, text) {
  if (!el) return false;
  el.focus();
  if (el.isContentEditable) {
    el.textContent = text;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    return true;
  }
  var proto = el.tagName === 'TEXTAREA'
    ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  var setter = Object.getOwnPropertyDescriptor(proto, 'value');
  if (!setter || !setter.set) return false;
  setter.set.call(el, text);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  return true;
})(__ELEMENT__, __TEXT__)
"""


def _find_browser_executable() -> str:
    """Locate Edge or Chrome: env override, PATH, then standard install dirs."""
    override = shutil.which("msedge") or shutil.which("chrome")
    if override:
        return override
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome", "/usr/bin/chromium-browser",
    ]
    for path in candidates:
        if Path(path).is_file():
            return path
    raise BrowserError(
        "backend_unavailable",
        t("br_exe_missing"))


class CdpBrowserPage:
    """BrowserPage over a real Edge/Chrome via CDP WebSocket."""

    def __init__(self, proc: subprocess.Popen, ws, http_base: str, target_id: str):
        self._proc = proc
        self._ws = ws
        self._http_base = http_base
        self._target_id = target_id
        self._next_id = 1
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle

    @classmethod
    async def launch(cls, url: str = "about:blank", browser_path: str | None = None,
                     timeout: float = 20.0, headless: bool = True,
                     user_data_dir: str | None = None) -> "CdpBrowserPage":
        """Launch Edge/Chrome and attach over CDP.

        headless=False runs a REAL visible browser window — the human sees
        what the agent does. `user_data_dir` opts into a persistent profile
        (the user's real profile only if they pass its path explicitly);
        without it a disposable temp profile is used either way.
        """
        exe = browser_path or _find_browser_executable()
        if user_data_dir is not None:
            profile = str(Path(user_data_dir).resolve())
        else:
            profile = tempfile.mkdtemp(prefix="oaset-cdp-")
        args = [exe]
        if headless:
            args.append("--headless=new")
        args += ["--remote-debugging-port=0", f"--user-data-dir={profile}",
                 "--no-first-run", "--disable-extensions", url]
        proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        ws_url = cls._read_devtools_ws(proc, timeout)
        http_base = ws_url.split("/devtools/")[0]
        target = await cls._pick_page_target(http_base, timeout)
        import websockets

        try:
            ws = await asyncio.wait_for(
                websockets.connect(target["webSocketDebuggerUrl"],
                                   max_size=64 * 1024 * 1024), timeout)
        except Exception as exc:
            proc.terminate()
            raise BrowserError("backend_unavailable",
                               t("br_ws_failed", exc=exc)) from exc
        page = cls(proc, ws, http_base, target["id"])
        await page._cmd("Page.enable")
        # Site-initiated downloads are denied from the start; exactly one
        # caller-approved download at a time is enabled via download().
        await page._cmd("Browser.setDownloadBehavior", {"behavior": "deny"})
        if url != "about:blank":
            await page.navigate(url)
        return page

    @staticmethod
    def _read_devtools_ws(proc: subprocess.Popen, timeout: float) -> str:
        import time as _time

        assert proc.stderr is not None
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            line = proc.stderr.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            text = line.decode("utf-8", errors="replace")
            if "DevTools listening on" in text:
                return text.split("DevTools listening on", 1)[1].strip()
        proc.terminate()
        raise BrowserError("backend_unavailable",
                           t("br_no_devtools"))

    @staticmethod
    async def _pick_page_target(http_base: str, timeout: float) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        last_error = ""
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{http_base}/json/list", timeout=5)
                    targets = resp.json()
                for target in targets:
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                        return target
                last_error = "no page target"
            except Exception as exc:
                last_error = str(exc)
            await asyncio.sleep(0.15)
        raise BrowserError("backend_unavailable",
                           t("br_no_page_target", err=last_error))

    # ------------------------------------------------------------- transport

    async def _cmd(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            return await self._raw_cmd(method, params)

    async def _raw_cmd(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one CDP command; caller must already hold `self._lock`."""
        request_id = self._next_id
        self._next_id += 1
        message = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        await self._ws.send(json.dumps(message))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=30)
            frame = json.loads(raw)
            if frame.get("id") != request_id:
                continue  # CDP event (Page.loadEventFired etc.)
            if "error" in frame:
                err = frame["error"]
                raise BrowserError("cdp_error",
                                   f"{method} failed: {err.get('message', err)}")
            return frame.get("result", {})

    # ---------------------------------------------------------------- helpers

    async def _eval(self, expression: str) -> Any:
        result = await self._cmd("Runtime.evaluate",
                                 {"expression": expression, "returnByValue": True})
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"].get("exception", {})
            raise BrowserError("page_error",
                               str(detail.get("description", detail)))
        return result.get("result", {}).get("value")

    def _element_expr(self, handle: str) -> str:
        """JS expression yielding the element for a minted handle."""
        if handle.startswith("oaset:"):
            index = int(handle.split(":", 1)[1])
            return f'document.querySelector(\'[data-oaset-h="{index}"]\')'
        if handle.startswith("css:"):
            selector = handle[4:]
            return f"document.querySelector({json.dumps(selector)})"
        raise BrowserError("element_not_found", f"unrecognized handle: {handle!r}")

    # ------------------------------------------------------------ BrowserPage

    async def observe(self) -> dict[str, Any]:
        data = await self._eval(OBSERVE_JS)
        elements = [
            PageElement(handle=f"oaset:{item['i']}", tag=item["tag"],
                        text=item["text"],
                        attributes={"id": item["id"], "name": item["name"]},
                        rect=item["rect"])
            for item in data["elements"]
        ]
        return {"url": data["url"], "title": data["title"], "elements": elements}

    async def find(self, selector: str) -> PageElement | None:
        expression = FIND_JS.replace("__ELEMENT__",
                                     self._element_expr(f"css:{selector}"))
        info = await self._eval(expression)
        if info is None:
            return None
        return PageElement(handle=f"css:{selector}", tag=info["tag"],
                           text=info["text"], rect=info["rect"])

    async def click(self, handle: str) -> bool:
        expression = CLICK_JS.replace("__ELEMENT__", self._element_expr(handle))
        return bool(await self._eval(expression))

    async def type_text(self, handle: str, text: str) -> bool:
        expression = (TYPE_JS
                      .replace("__ELEMENT__", self._element_expr(handle))
                      .replace("__TEXT__", json.dumps(text)))
        return bool(await self._eval(expression))

    async def screenshot(self) -> Screenshot:
        shot = await self._cmd("Page.captureScreenshot", {"format": "png"})
        metrics = await self._cmd("Page.getLayoutMetrics")
        viewport = metrics.get("layoutViewport", {}) or metrics.get("cssVisualViewport", {})
        return Screenshot(mime="image/png",
                          data=base64.b64decode(shot["data"]),
                          width=int(viewport.get("clientWidth",
                                                 viewport.get("width", 0)) or 0),
                          height=int(viewport.get("clientHeight",
                                                  viewport.get("height", 0)) or 0))

    async def navigate(self, url: str) -> None:
        await self._cmd("Page.navigate", {"url": url})
        await asyncio.sleep(0.3)  # let load settle; events are ignored by _cmd

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception:
            pass
        if self._proc.poll() is None:
            self._proc.terminate()

    # ------------------------------------------------------- download/upload

    async def download(self, url: str, dest_dir: Path,
                       timeout: float = 60.0) -> "DownloadResult":
        """Enable exactly one browser download into `dest_dir`, trigger it,
        await its completion event and verify the file exists on disk.

        The default-deny policy is restored before returning, whatever the
        outcome — an approved download cannot leave downloading enabled.
        """
        dest_dir = Path(dest_dir).resolve()
        if not dest_dir.is_dir():
            raise BrowserError("download_dest_missing",
                               t("br_download_dest_missing", dest=dest_dir))
        before = {p.name for p in dest_dir.iterdir()}
        async with self._lock:
            await self._raw_cmd("Browser.setDownloadBehavior", {
                "behavior": "allowAndName", "downloadPath": str(dest_dir),
                "eventsEnabled": True})
            try:
                await self._raw_cmd("Page.navigate", {"url": url})
                suggested, out_path = await self._await_download(
                    dest_dir, before, timeout)
            finally:
                try:
                    await self._raw_cmd("Browser.setDownloadBehavior",
                                        {"behavior": "deny"})
                except Exception:
                    pass  # the deny-restore must never mask the real outcome
        if out_path is None:
            raise BrowserError("download_failed", t("br_download_not_complete", url=url))
        return DownloadResult(path=out_path, suggested_filename=suggested,
                              size_bytes=out_path.stat().st_size, url=url)

    async def _await_download(self, dest_dir: Path, before: set[str],
                              timeout: float) -> tuple[str, Path | None]:
        """Read CDP frames until the download completes/cancels. Guid events
        name the file; the on-disk diff is the final authority."""
        import websockets

        suggested = ""
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                new_names = {p.name for p in dest_dir.iterdir()} - before
                if new_names:  # file landed even without a progress event
                    return suggested, dest_dir / sorted(new_names)[-1]
                continue
            except websockets.exceptions.ConnectionClosed as exc:
                raise BrowserError("backend_unavailable",
                                   t("br_ws_closed", exc=exc)) from exc
            frame = json.loads(raw)
            method = frame.get("method", "")
            params = frame.get("params", {})
            if method == "Browser.downloadWillBegin":
                suggested = str(params.get("suggestedFilename", ""))
            elif method == "Browser.downloadProgress":
                state = params.get("state")
                if state == "completed":
                    new_names = {p.name for p in dest_dir.iterdir()} - before
                    if not new_names:
                        # guid-named by the browser; re-scan with the guid
                        guid = str(params.get("guid", ""))
                        candidates = [p for p in dest_dir.iterdir()
                                      if guid and guid in p.name]
                        new_names = {p.name for p in candidates}
                    if not new_names:
                        raise BrowserError("download_failed",
                                           t("br_download_no_file"))
                    return suggested, dest_dir / sorted(new_names)[-1]
                if state == "canceled":
                    raise BrowserError("download_failed",
                                       t("br_download_cancelled"))
                # inProgress and unknown states: keep reading frames
        raise BrowserError("download_timeout",
                           t("br_download_timeout", timeout=timeout, name=suggested or 'download'))

    async def upload(self, handle: str, files: list[Path]) -> bool:
        """Attach local files to a page <input type=file> via
        DOM.setFileInputFiles — the only route browsers allow (JS cannot)."""
        if not files:
            raise BrowserError("upload_empty", t("br_upload_empty"))
        if len(files) > MAX_UPLOAD_FILES:
            raise BrowserError("upload_too_many",
                               t("br_upload_too_many", max=MAX_UPLOAD_FILES))
        for f in files:
            if not Path(f).is_file():
                raise BrowserError("upload_file_missing",
                                   t("br_upload_missing", path=f))
        selector = self._handle_selector(handle)
        await self._cmd("DOM.enable")
        doc = await self._cmd("DOM.getDocument", {})
        root = doc.get("root", {}).get("nodeId", 0)
        node = await self._cmd("DOM.querySelector",
                               {"nodeId": root, "selector": selector})
        node_id = node.get("nodeId", 0)
        if not node_id:
            raise BrowserError("element_not_found",
                               t("br_no_file_input", handle=handle))
        await self._cmd("DOM.setFileInputFiles",
                        {"files": [str(Path(f).resolve()) for f in files],
                         "nodeId": node_id})
        return True

    def _handle_selector(self, handle: str) -> str:
        if handle.startswith("oaset:"):
            index = int(handle.split(":", 1)[1])
            return f'[data-oaset-h="{index}"]'
        if handle.startswith("css:"):
            return handle[4:]
        raise BrowserError("element_not_found", f"unrecognized handle: {handle!r}")


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    suggested_filename: str
    size_bytes: int
    url: str
