"""BrowserProvider phase 1: protocol + gated wrapper, end-to-end on a fake page."""

from __future__ import annotations

import pytest

from oaset.computer import BrowserError, GatedBrowserPage, PageElement, Screenshot
from oaset.network import NetworkPolicy

# ------------------------------------------------------------------ fake page

class FakePage:
    """In-memory page: enough DOM state to prove observe/find/click/type/screenshot
    really mutate (or provably do not mutate) through the gated wrapper."""

    def __init__(self) -> None:
        self.url = "https://example.test/form"
        self.title = "Form"
        self.elements = {
            "h-search": PageElement(handle="h-search", tag="input",
                                    attributes={"type": "search"},
                                    rect={"x": 10, "y": 20, "w": 120, "h": 24}),
            "h-submit": PageElement(handle="h-submit", tag="button", text="Submit",
                                    rect={"x": 10, "y": 60, "w": 80, "h": 24}),
        }
        self.values: dict[str, str] = {}
        self.clicked: list[str] = []
        self.closed = False

    async def observe(self) -> dict:
        return {"url": self.url, "title": self.title,
                "elements": list(self.elements.values())}

    async def find(self, selector: str) -> PageElement | None:
        return self.elements.get(selector)

    async def click(self, handle: str) -> bool:
        if handle not in self.elements:
            return False
        self.clicked.append(handle)
        return True

    async def type_text(self, handle: str, text: str) -> bool:
        if handle not in self.elements:
            return False
        self.values[handle] = text
        return True

    async def screenshot(self) -> Screenshot:
        return Screenshot(mime="image/png", data=b"\x89PNG-fake-viewport",
                          width=640, height=480)

    async def close(self) -> None:
        self.closed = True


class AllowGate:
    async def request(self, tool_name, level, summary, preview=None):
        return "always"


class DenyGate:
    async def request(self, tool_name, level, summary, preview=None):
        return "deny"


# ----------------------------------------------------------------- read paths

async def test_observe_find_screenshot_allowed_under_pull_only():
    page = FakePage()
    gated = GatedBrowserPage(page, AllowGate(), NetworkPolicy("pull_only"))
    snap = await gated.observe()
    assert snap["url"] == "https://example.test/form"
    assert {e.handle for e in snap["elements"]} == {"h-search", "h-submit"}
    el = await gated.find("h-submit")
    assert el is not None and el.text == "Submit" and el.rect["x"] == 10
    shot = await gated.screenshot()
    assert shot.mime == "image/png" and shot.data.startswith(b"\x89PNG")
    assert shot.summary().startswith("image/png 640x480")


async def test_local_only_blocks_entire_provider():
    page = FakePage()
    gated = GatedBrowserPage(page, AllowGate(), NetworkPolicy("local_only"))
    actions = [
        lambda: gated.observe(),
        lambda: gated.find("h-search"),
        lambda: gated.screenshot(),
        lambda: gated.click("h-submit"),
        lambda: gated.type_text("h-search", "x"),
    ]
    for action in actions:
        with pytest.raises(BrowserError) as excinfo:
            await action()
        assert excinfo.value.code == "network_blocked"
    assert page.clicked == [] and page.values == {}  # nothing reached the page


# ------------------------------------------------------------- action paths

async def test_click_and_type_require_full_mode_even_when_gate_allows():
    page = FakePage()
    gated = GatedBrowserPage(page, AllowGate(), NetworkPolicy("pull_only"))
    with pytest.raises(BrowserError) as excinfo:
        await gated.click("h-submit")
    assert excinfo.value.code == "network_blocked"
    assert "full" in str(excinfo.value)
    assert page.clicked == []


async def test_click_allowed_with_full_mode_and_gate():
    page = FakePage()
    gated = GatedBrowserPage(page, AllowGate(), NetworkPolicy("full"))
    assert await gated.click("h-submit") is True
    assert page.clicked == ["h-submit"]


async def test_type_text_allowed_with_full_mode_and_gate():
    page = FakePage()
    gated = GatedBrowserPage(page, AllowGate(), NetworkPolicy("full"))
    assert await gated.type_text("h-search", "hello world") is True
    assert page.values["h-search"] == "hello world"


async def test_gate_denial_leaves_page_untouched():
    page = FakePage()
    gated = GatedBrowserPage(page, DenyGate(), NetworkPolicy("full"))
    with pytest.raises(BrowserError) as excinfo:
        await gated.click("h-submit")
    assert excinfo.value.code == "permission_denied"
    with pytest.raises(BrowserError):
        await gated.type_text("h-search", "nope")
    assert page.clicked == [] and page.values == {}


async def test_stale_handle_returns_false_not_exception():
    page = FakePage()
    gated = GatedBrowserPage(page, AllowGate(), NetworkPolicy("full"))
    assert await gated.click("h-gone") is False
    assert await gated.type_text("h-gone", "x") is False


async def test_close_passes_through():
    page = FakePage()
    gated = GatedBrowserPage(page, AllowGate(), NetworkPolicy("local_only"))
    await gated.close()  # closing is allowed even under local_only
    assert page.closed is True
