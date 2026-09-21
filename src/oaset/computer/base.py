"""BrowserPage protocol + the gated wrapper every browser action passes through.

Phase-1 capability set: observe / find / click / type_text / screenshot.

Security contract (see git history: ARCHITECTURE §5.3, deleted 2026-09-13):

- observe / find / screenshot are read-only on the CURRENT page: allowed by
  NetworkPolicy under pull_only and full; blocked under local_only.
- click / type_text can submit data or navigate — they are push-class under
  the frozen egress matrix: NetworkPolicy must allow `push` (i.e. mode
  "full"), AND every call goes through the unified PermissionGate
  ("browser_click"/"browser_type", level exec) so the TUI asks the user and
  --yolo/one-shot semantics apply like any other tool.
- download moves remote data onto host disk: pull-class egress minimum plus
  a write-level approval naming url and destination; the driver denies all
  site-initiated downloads outside an approved download() call.
- upload sends host file contents to the site: push-class egress plus an
  exec-level approval listing the exact files; missing paths are rejected
  before the approval round-trip.
- A denial raises BrowserError(code="permission_denied") and the page state
  is untouched — the gate runs BEFORE the underlying page call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from oaset.i18n import t


class BrowserError(Exception):
    """Structured browser failure; `code` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PageElement:
    """One addressable node on the page.

    `handle` is the stable reference for subsequent click/type_text calls
    (opaque to callers — browsers and desktop providers mint their own).
    `rect` is viewport CSS pixels {x, y, w, h}; screenshots carry the same
    coordinate space so a click target can be cross-checked visually.
    """

    handle: str
    tag: str
    text: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    rect: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Screenshot:
    mime: str  # e.g. "image/png"
    data: bytes
    width: int = 0
    height: int = 0

    def summary(self) -> str:
        return f"{self.mime} {self.width}x{self.height} ({len(self.data)} bytes)"


class BrowserPage(Protocol):
    """Backend-neutral page interface (fake pages for tests, real driver later)."""

    async def observe(self) -> dict[str, Any]:
        """Current page snapshot: {url, title, elements: [PageElement...]}."""
        ...

    async def find(self, selector: str) -> PageElement | None:
        """Locate one element by backend selector; None when absent."""
        ...

    async def click(self, handle: str) -> bool:
        """Click a previously observed/found element. False when the handle
        is gone (stale)."""
        ...

    async def type_text(self, handle: str, text: str) -> bool:
        """Type text into a focusable element. False when stale."""
        ...

    async def screenshot(self) -> Screenshot:
        """Capture the current viewport as an image."""
        ...

    async def download(self, url: str, dest_dir: Any) -> Any:
        """One caller-approved download into dest_dir (driver-level)."""
        ...

    async def upload(self, handle: str, files: list[Any]) -> bool:
        """Attach local files to a page file input (driver-level)."""
        ...

    async def close(self) -> None:
        ...


def _denied(action: str) -> BrowserError:
    return BrowserError("permission_denied", f"browser {action} denied by user/policy")


class GatedBrowserPage:
    """Wrap any BrowserPage with the unified approval gate + egress policy.

    The gate object is the same PermissionGate the ToolRegistry uses
    (UIGate in the TUI, AutoGate under --yolo, ReadOnlyGate in one-shot),
    so browser actions behave like any other tool permission.
    """

    def __init__(self, page: BrowserPage, gate: Any, policy: Any, log: Any = None):
        self.page = page
        self.gate = gate
        self.policy = policy
        self._log = log

    # ------------------------------------------------------------- read-only

    def _require_not_local_only(self, action: str) -> None:
        if not self.policy.allows("pull"):  # local_only blocks everything
            raise BrowserError(
                "network_blocked",
                f"browser {action} requires at least pull_only network mode "
                f"(current: {getattr(self.policy, 'mode', '?')})",
            )

    async def observe(self) -> dict[str, Any]:
        self._require_not_local_only("observe")
        return await self.page.observe()

    async def find(self, selector: str) -> PageElement | None:
        self._require_not_local_only("find")
        return await self.page.find(selector)

    async def screenshot(self) -> Screenshot:
        self._require_not_local_only("screenshot")
        return await self.page.screenshot()

    # -------------------------------------------------- state-changing actions

    def _require_push(self, action: str) -> None:
        if not self.policy.allows("pull"):
            raise BrowserError(
                "network_blocked",
                f"browser {action} requires at least pull_only network mode "
                f"(current: {getattr(self.policy, 'mode', '?')})",
            )
        if not self.policy.allows("push"):
            raise BrowserError(
                "network_blocked",
                f"browser {action} can submit data/navigate and is push-class: "
                f"set [network] mode = \"full\" to enable (current: "
                f"{getattr(self.policy, 'mode', '?')})",
            )

    async def _approve(self, action: str, summary: str, preview: str | None = None) -> None:
        decision = await self.gate.request(f"browser_{action}", "exec", summary, preview)
        if not decision or str(decision).lower() in ("deny", "denied"):
            if self._log:
                self._log(f"browser {action} denied by gate")
            raise _denied(action)

    async def click(self, handle: str) -> bool:
        self._require_push("click")
        await self._approve("click", f"Click element {handle}",
                            preview=handle)
        return await self.page.click(handle)

    async def type_text(self, handle: str, text: str) -> bool:
        self._require_push("type_text")
        preview = text if len(text) <= 200 else text[:200] + "…"
        await self._approve("type_text", f"Type {len(text)} chars into element {handle}",
                            preview=preview)
        return await self.page.type_text(handle, text)

    # --------------------------------------------------- download/upload policy

    MAX_UPLOAD_FILES = 10

    async def download(self, url: str, dest_dir) -> Any:
        """One caller-approved download into `dest_dir`.

        Downloads move remote data onto the host filesystem, so they need at
        least pull_only egress AND a write-level approval that names both the
        source URL and the destination directory. The underlying driver keeps
        site-initiated downloads denied at all other times.
        """
        from pathlib import Path as _Path

        self._require_not_local_only("download")
        dest = _Path(dest_dir)
        if not dest.is_dir():
            raise BrowserError("download_dest_missing",
                               t("br_download_dest_missing", dest=dest))
        await self._approve("download", f"Download {url} into {dest}",
                            preview=f"url: {url}\ndest: {dest}")
        return await self.page.download(url, dest)

    async def upload(self, handle: str, files) -> bool:
        """Attach local files to a page file input — push-class (the site
        receives host file contents) and exec-level, with existence checks
        BEFORE asking the user so a typo'd path never reaches approval."""
        import os
        from pathlib import Path as _Path

        self._require_push("upload")
        checked: list[_Path] = []
        for f in files:
            p = _Path(f)
            if not p.is_file():
                raise BrowserError("upload_file_missing",
                                   t("br_upload_missing", path=p))
            checked.append(p)
        if not checked:
            raise BrowserError("upload_empty", t("br_upload_empty"))
        if len(checked) > self.MAX_UPLOAD_FILES:
            raise BrowserError("upload_too_many",
                               t("br_upload_too_many", max=self.MAX_UPLOAD_FILES))
        total = sum(os.path.getsize(p) for p in checked)
        listing = "\n".join(f"{p.name} ({os.path.getsize(p)} bytes)"
                            for p in checked)
        await self._approve("upload",
                            f"Upload {len(checked)} file(s) "
                            f"({total} bytes total) via {handle}",
                            preview=listing)
        return await self.page.upload(handle, checked)

    async def close(self) -> None:
        await self.page.close()
