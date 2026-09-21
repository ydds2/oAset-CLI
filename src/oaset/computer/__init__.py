"""Computer-use capability layer (DESK-01/02).

Browser and desktop automation share NOTHING but these base types: the plan
forbids mixing browser selectors/coordinates/permissions with desktop ones.
Phase 1 ships the BrowserPage protocol + the approval/policy-gated wrapper;
the Windows UIA desktop provider comes later in this package.
"""

from oaset.computer.base import (
    BrowserError,
    BrowserPage,
    GatedBrowserPage,
    PageElement,
    Screenshot,
)
from oaset.computer.cdp import CdpBrowserPage, DownloadResult
from oaset.computer.desktop import (
    DesktopElement,
    DesktopError,
    DesktopPage,
    DesktopScreenshot,
    DesktopWindow,
    GatedDesktopPage,
    UiaBackend,
    UiaDesktopPage,
)
from oaset.computer.input import FakeInputDriver, WindowsInputDriver

# Real UIA backend registers itself on Windows; elsewhere stays unavailable.
from oaset.computer.uia_backend import register_default_backend as _register_uia
from oaset.computer.use import ComputerUseSession
from oaset.computer.vision import VisionFallbackPage

try:
    _register_uia()
except Exception:  # backend registration must never break the import
    pass

__all__ = [
    "BrowserError",
    "BrowserPage",
    "CdpBrowserPage",
    "DownloadResult",
    "GatedBrowserPage",
    "PageElement",
    "Screenshot",
    "DesktopElement",
    "DesktopError",
    "DesktopPage",
    "DesktopScreenshot",
    "DesktopWindow",
    "GatedDesktopPage",
    "UiaBackend",
    "UiaDesktopPage",
    "VisionFallbackPage",
    "FakeInputDriver",
    "WindowsInputDriver",
    "ComputerUseSession",
]
