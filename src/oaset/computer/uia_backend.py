"""Real Windows UIA backend — pure-ctypes COM, no third-party dependencies.

Implements UiaBackend (computer/desktop.py) over IUIAutomation:

- list_windows: user32.EnumWindows (visible, titled) + GetWindowRect/DPI
- elements/find_element: ElementFromHandle + FindFirst/FindAll with a
  UIA_NamePropertyId condition; handles are minted per enumeration
  ("hwnd:Name") and also resolvable by bare Name for the common case
- click_element: UIA InvokePattern (semantic activation, not a synthetic
  coordinate click — coordinate injection stays with VisionFallback)
- type_text: UIA ValuePattern SetValue (the accessibility-native way to put
  text into a control; SendInput keystroke fallback is NOT implemented yet)
- capture: GDI BitBlt of the window rect → BMP bytes (no Pillow dependency)

Everything is verified against REAL windows in tests/test_uia_real.py.
Vtable indices below follow the IUIAutomation interface order; the
real-machine tests are the contract that they are right.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes as wt

from oaset.computer.desktop import (
    DesktopElement,
    DesktopError,
    DesktopScreenshot,
    DesktopWindow,
    _rect,
)
from oaset.i18n import t

# ------------------------------------------------------------------ COM ids

CLSID_CUIAutomation = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
# IIDs extracted from uiautomationcore.dll's typelib (comtypes probe,
# 2026-09-10) — verified against the registry CLSID and live CoCreateInstance.
IID_IUIAutomation = "{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}"
IID_IUIAutomationElement = "{d22108aa-8ac5-49a5-837b-37bbb3d7591e}"
IID_IUIAutomationInvokePattern = "{fb377fbe-8ea6-46d5-9c73-6499642d3059}"
IID_IUIAutomationValuePattern = "{a94cd8b1-0844-4cd6-9d2d-640537ab39e9}"

# IUIAutomation vtable slots (0-2 are IUnknown; authoritative order from the
# uiautomationcore typelib: CompareElements, CompareRuntimeIds come FIRST)
VT_GET_ROOT_ELEMENT = 5
VT_ELEMENT_FROM_HANDLE = 6
VT_CREATE_PROPERTY_CONDITION = 23
VT_CREATE_TRUE_CONDITION = 21
VT_GET_CONTROL_VIEW_WALKER = 14
# IUIAutomationTreeWalker (after IUnknown)
VT_WALKER_FIRST_CHILD = 4
VT_WALKER_NEXT_SIBLING = 6

# IUIAutomationElement vtable slots
VT_SET_FOCUS = 3
VT_FIND_FIRST = 5
VT_FIND_ALL = 6
VT_GET_CURRENT_PATTERN = 16
VT_GET_CURRENT_PATTERN_AS = 14
VT_CURRENT_CONTROL_TYPE = 21
VT_CURRENT_NAME = 23
VT_CURRENT_BOUNDING_RECTANGLE = 43

# Pattern vtable slots (after IUnknown)
VT_INVOKE = 3
VT_VALUE_SET = 3

UIA_NAME_PROPERTY_ID = 30005
TREE_SCOPE_CHILDREN = 2
TREE_SCOPE_DESCENDANTS = 4
UIA_INVOKE_PATTERN_ID = 10000
UIA_VALUE_PATTERN_ID = 10002

VT_BSTR = 8
COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    """CLSIDFromString is UNICODE-only: c_wchar_p, and the hr is checked."""
    guid = _GUID()
    hr = ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    if hr != 0:
        raise DesktopError("backend_unavailable",
                           f"CLSIDFromString({text}) failed hr=0x{hr & 0xffffffff:08x}")
    return guid


class _VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("wReserved1", ctypes.c_ushort),
                ("wReserved2", ctypes.c_ushort), ("wReserved3", ctypes.c_ushort),
                ("bstrVal", ctypes.c_void_p)]


def _com_call(vtbl_index: int, restype, *args):
    """Build a WINFUNCTYPE from the object's vtable slot and invoke it."""
    def factory(this, *call_args):
        return restype(call_args)
    return factory


class _ComPtr:
    """Thin COM pointer with vtable-call helper and Release on GC."""

    def __init__(self, ptr: int):
        self.ptr = ptr

    def call(self, slot: int, restype, *args):
        vtbl = ctypes.cast(self.ptr, ctypes.POINTER(ctypes.c_void_p))[0]
        func_addr = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[slot]
        prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p,
                                        *[a[0] for a in args])
        func = prototype(func_addr)
        return func(self.ptr, *[a[1] for a in args])

    def __del__(self):
        if getattr(self, "ptr", 0):
            try:
                self.call(2, ctypes.HRESULT)  # Release
            except Exception:
                pass
            self.ptr = 0


def _bstr(text: str) -> int:
    SysAllocString = ctypes.windll.oleaut32.SysAllocString
    SysAllocString.argtypes = [ctypes.c_wchar_p]
    SysAllocString.restype = ctypes.c_void_p
    return SysAllocString(text) or 0


def _free_bstr(ptr: int) -> None:
    SysFreeString = ctypes.windll.oleaut32.SysFreeString
    SysFreeString.argtypes = [ctypes.c_void_p]
    SysFreeString(ctypes.c_void_p(ptr))


def _read_bstr(ptr: int) -> str:
    try:
        return ctypes.wstring_at(ptr) if ptr else ""
    finally:
        _free_bstr(ptr)


# ------------------------------------------------------------------- backend

_SAFEARRAY_SIGNATURES_DONE = False


def _safe_array_pointers(safearray: int) -> list[int]:
    """Read a SAFEARRAY of IUnknown* into raw pointer values.

    All SafeArray* entry points get EXPLICIT argtypes: default marshalling
    of c_void_p arguments proved unreliable here (access violations).
    """
    global _SAFEARRAY_SIGNATURES_DONE
    oleaut32 = ctypes.windll.oleaut32
    if not _SAFEARRAY_SIGNATURES_DONE:
        oleaut32.SafeArrayGetLBound.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                ctypes.POINTER(ctypes.c_long)]
        oleaut32.SafeArrayGetLBound.restype = ctypes.c_long
        oleaut32.SafeArrayGetUBound.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                ctypes.POINTER(ctypes.c_long)]
        oleaut32.SafeArrayGetUBound.restype = ctypes.c_long
        oleaut32.SafeArrayAccessData.argtypes = [ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_void_p)]
        oleaut32.SafeArrayAccessData.restype = ctypes.c_long
        oleaut32.SafeArrayUnaccessData.argtypes = [ctypes.c_void_p]
        oleaut32.SafeArrayUnaccessData.restype = ctypes.c_long
        oleaut32.SafeArrayDestroy.argtypes = [ctypes.c_void_p]
        oleaut32.SafeArrayDestroy.restype = ctypes.c_long
        _SAFEARRAY_SIGNATURES_DONE = True
    if not safearray:
        return []
    lower, upper = ctypes.c_long(), ctypes.c_long()
    if oleaut32.SafeArrayGetLBound(ctypes.c_void_p(safearray), 1, ctypes.byref(lower)) != 0:
        return []
    if oleaut32.SafeArrayGetUBound(ctypes.c_void_p(safearray), 1, ctypes.byref(upper)) != 0:
        return []
    data = ctypes.c_void_p()
    out: list[int] = []
    if oleaut32.SafeArrayAccessData(ctypes.c_void_p(safearray), ctypes.byref(data)) == 0:
        try:
            ptr_array = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))
            for i in range(upper.value - lower.value + 1):
                if ptr_array[i]:
                    out.append(ptr_array[i])
        finally:
            oleaut32.SafeArrayUnaccessData(ctypes.c_void_p(safearray))
    oleaut32.SafeArrayDestroy(ctypes.c_void_p(safearray))
    return out


class UiaComBackend:
    """UiaBackend implemented over real IUIAutomation + GDI."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise DesktopError("backend_unavailable", t("uia_windows_only"))
        self.ole32 = ctypes.windll.ole32
        hr = self.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        self._need_couninit = hr == 0  # S_OK means we initialized this apartment
        clsid, iid = _guid(CLSID_CUIAutomation), _guid(IID_IUIAutomation)
        punk = ctypes.c_void_p()
        CoCreateInstance = self.ole32.CoCreateInstance
        CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_ulong,
                                     ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
        CoCreateInstance.restype = ctypes.HRESULT
        hr = CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER,
                              ctypes.byref(iid), ctypes.byref(punk))
        if hr != 0 or not punk.value:
            raise DesktopError("backend_unavailable",
                               f"CoCreateInstance(CUIAutomation) failed hr=0x{hr & 0xffffffff:08x}")
        self._uia = _ComPtr(punk.value)
        self._handles: dict[str, dict] = {}  # minted handle -> resolution info

    # -------------------------------------------------------------- plumbing

    def _element_from_hwnd(self, hwnd: int) -> _ComPtr:
        out = ctypes.c_void_p()
        hr = self._uia.call(VT_ELEMENT_FROM_HANDLE, ctypes.HRESULT,
                            (ctypes.c_void_p, hwnd), (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(out)))
        if hr != 0 or not out.value:
            raise DesktopError("element_not_found", f"ElementFromHandle({hwnd}) failed hr=0x{hr & 0xffffffff:08x}")
        return _ComPtr(out.value)

    def _true_condition(self) -> int:
        condition = ctypes.c_void_p()
        hr = self._uia.call(VT_CREATE_TRUE_CONDITION, ctypes.HRESULT,
                            (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(condition)))
        if hr != 0 or not condition.value:
            raise DesktopError("backend_unavailable", "CreateTrueCondition failed")
        return condition.value

    def _control_walker(self) -> int:
        walker = ctypes.c_void_p()
        hr = self._uia.call(VT_GET_CONTROL_VIEW_WALKER, ctypes.HRESULT,
                            (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(walker)))
        if hr != 0 or not walker.value:
            raise DesktopError("backend_unavailable", "get_ControlViewWalker failed")
        return walker.value

    def _find_all_children(self, root: _ComPtr) -> list[_ComPtr]:
        """Enumerate children via the ControlView TreeWalker.

        The SAFEARRAY returned by FindAll proved unstable in this toolchain
        (access violations inside SafeArrayGetLBound on some windows), so we
        walk first-child/next-sibling instead — one element out-param per call.
        CreatePropertyCondition's VARIANT marshalling is likewise avoided;
        name filtering is done locally by the caller.
        """
        walker_ptr = ctypes.c_void_p()
        hr = self._uia.call(VT_GET_CONTROL_VIEW_WALKER, ctypes.HRESULT,
                            (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(walker_ptr)))
        if hr != 0 or not walker_ptr.value:
            raise DesktopError("backend_unavailable", "get_ControlViewWalker failed")
        walker = _ComPtr(walker_ptr.value)  # wrap ONCE: every Release must pair one AddRef
        out: list[_ComPtr] = []
        child = ctypes.c_void_p()
        hr = walker.call(VT_WALKER_FIRST_CHILD, ctypes.HRESULT,
                         (ctypes.c_void_p, root.ptr),
                         (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(child)))
        guard = 0
        while hr == 0 and child.value and guard < 200:
            out.append(_ComPtr(child.value))
            nxt = ctypes.c_void_p()
            hr = walker.call(VT_WALKER_NEXT_SIBLING, ctypes.HRESULT,
                             (ctypes.c_void_p, child.value),
                             (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(nxt)))
            child = nxt
            guard += 1
        return out

    def _find_by_name(self, root: _ComPtr, name: str,
                      max_depth: int = 4, max_nodes: int = 500) -> _ComPtr | None:
        """BFS the subtree comparing CurrentName locally."""
        queue: list[tuple[_ComPtr, int]] = [(root, 0)]
        visited = 0
        while queue and visited < max_nodes:
            element, depth = queue.pop(0)
            visited += 1
            if depth > 0 and self._element_name(element) == name:
                return element
            if depth >= max_depth:
                continue
            for child in self._find_all_children(element):
                queue.append((child, depth + 1))
        return None

    def _element_name(self, element: _ComPtr) -> str:
        out = ctypes.c_void_p()
        element.call(VT_CURRENT_NAME, ctypes.HRESULT,
                     (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(out)))
        return _read_bstr(out.value or 0)

    def _element_control_type(self, element: _ComPtr) -> int:
        out = ctypes.c_int()
        element.call(VT_CURRENT_CONTROL_TYPE, ctypes.HRESULT,
                     (ctypes.POINTER(ctypes.c_int), ctypes.byref(out)))
        return out.value

    def _element_rect(self, element: _ComPtr) -> dict:
        rect = wt.RECT()
        element.call(VT_CURRENT_BOUNDING_RECTANGLE, ctypes.HRESULT,
                     (ctypes.POINTER(wt.RECT), ctypes.byref(rect)))
        return _rect(rect.left, rect.top, rect.right - rect.left,
                     rect.bottom - rect.top, scale=1.0, display=0)

    def _find_first(self, root: _ComPtr, name: str) -> _ComPtr | None:
        return self._find_by_name(root, name)

    # ---------------------------------------------------------- window list

    def list_windows(self) -> list[DesktopWindow]:
        user32 = ctypes.windll.user32
        results: list[DesktopWindow] = []
        foreground = user32.GetForegroundWindow()
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        def on_window(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value
            if not title.strip():
                return True
            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            hdc = user32.GetDC(hwnd)
            dpi = user32.GetDpiForWindow(hwnd) if hasattr(user32, "GetDpiForWindow") else 96
            if hdc:
                user32.ReleaseDC(hwnd, hdc)
            results.append(DesktopWindow(
                handle=str(hwnd), title=title, pid=int(pid.value),
                rect=_rect(rect.left, rect.top, rect.right - rect.left,
                           rect.bottom - rect.top,
                           scale=round(dpi / 96.0, 2), display=0),
                foreground=(hwnd == foreground),
            ))
            return True

        user32.EnumWindows(enum_proc(on_window), 0)
        return results

    # ------------------------------------------------------------- elements

    def _mint(self, window_handle: str, element: _ComPtr, control_type_id: int) -> DesktopElement:
        name = self._element_name(element)
        handle = f"{window_handle}:{name}" if name else f"{window_handle}:{control_type_id}:{id(element) & 0xffff}"
        # cache OWNS the element reference: nameless controls (e.g. a bare
        # Win32 EDIT) cannot be re-found by name, so interaction goes through
        # the cached pointer directly
        self._handles[handle] = {"window": window_handle, "name": name,
                                 "control_type_id": control_type_id,
                                 "element": element}
        return DesktopElement(handle=handle, window_handle=window_handle,
                              control_type=self._type_name(control_type_id),
                              name=name, rect=self._element_rect(element))

    @staticmethod
    def _type_name(control_type_id: int) -> str:
        well = {50000: "Button", 50004: "Edit", 50037: "DataItem",
                50038: "Document", 50033: "MenuItem", 50032: "Menu",
                50019: "Window", 50003: "Dialog", 50012: "Text",
                50026: "ListItem", 50009: "Header"}
        return well.get(control_type_id, f"Type{control_type_id}")

    def elements(self, window_handle: str) -> list[DesktopElement]:
        hwnd = int(window_handle)
        root = self._element_from_hwnd(hwnd)
        out: list[DesktopElement] = []
        for element in self._find_all_children(root):
            type_id = self._element_control_type(element)
            out.append(self._mint(window_handle, element, type_id))
        return out

    def find_element(self, window_handle: str, selector: str) -> DesktopElement | None:
        cached = self._handles.get(selector)
        name = cached["name"] if cached else selector
        root = self._element_from_hwnd(int(window_handle))
        found = self._find_first(root, name)
        if found is None:
            return None
        return self._mint(window_handle, found,
                         self._element_control_type(found))

    # ---------------------------------------------------------- interaction

    def _pattern(self, element: _ComPtr, pattern_id: int, iid_text: str) -> _ComPtr | None:
        """GetCurrentPattern → IUnknown, then QueryInterface to the typed
        pattern (GetCurrentPatternAs's REFIID marshalling misbehaved here —
        returned S_OK patterns that Invoke no-op'd; the two-step path is the
        one verified against a real MessageBox)."""
        unknown = ctypes.c_void_p()
        hr = element.call(VT_GET_CURRENT_PATTERN, ctypes.HRESULT,
                          (ctypes.c_int, pattern_id),
                          (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(unknown)))
        if hr != 0 or not unknown.value:
            return None
        iid = _guid(iid_text)
        typed = ctypes.c_void_p()
        hr = _ComPtr(unknown.value).call(0, ctypes.HRESULT,  # QueryInterface
                                         (ctypes.c_void_p, ctypes.cast(ctypes.byref(iid), ctypes.c_void_p).value),
                                         (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(typed)))
        if hr != 0 or not typed.value:
            return None
        return _ComPtr(typed.value)

    def _resolve(self, handle: str) -> tuple[str, str]:
        info = self._handles.get(handle)
        if info:
            return info["window"], info["name"]
        # fall back: "hwnd:Name" minted handles parse directly
        window, _, name = handle.partition(":")
        return window, name

    def _cached_element(self, handle: str) -> _ComPtr | None:
        """Resolve a minted handle to its LIVE element (cache owns the ref);
        nameless controls are only reachable this way."""
        entry = self._handles.get(handle)
        if entry is None:
            return None
        element = entry.get("element")
        return element if element is not None and element.ptr else None

    def click_element(self, handle: str) -> bool:
        element = self._cached_element(handle)
        if element is None:
            window, name = self._resolve(handle)
            if not name:
                return False
            root = self._element_from_hwnd(int(window))
            element = self._find_first(root, name)
            if element is None:
                return False
        try:
            invoke = self._pattern(element, UIA_INVOKE_PATTERN_ID, IID_IUIAutomationInvokePattern)
        except OSError:
            return False  # stale element
        if invoke is None:
            try:
                element.call(VT_SET_FOCUS, ctypes.HRESULT)
            except OSError:
                pass
            return False
        try:
            hr = invoke.call(VT_INVOKE, ctypes.HRESULT)
        except OSError:
            return False
        return hr == 0

    def type_text(self, handle: str, text: str) -> bool:
        element = self._cached_element(handle)
        if element is None:
            window, name = self._resolve(handle)
            if not name:
                return False
            root = self._element_from_hwnd(int(window))
            element = self._find_first(root, name)
            if element is None:
                return False
        try:
            value = self._pattern(element, UIA_VALUE_PATTERN_ID, IID_IUIAutomationValuePattern)
        except OSError:
            return False  # stale element
        if value is None:
            return False
        bstr = _bstr(text)
        try:
            hr = value.call(VT_VALUE_SET, ctypes.HRESULT, (ctypes.c_void_p, bstr))
        except OSError:
            return False
        finally:
            _free_bstr(bstr)
        return hr == 0

    # -------------------------------------------------------------- capture

    def window_origin(self, window_handle: str | None) -> tuple[int, int]:
        """Physical-screen origin (left, top) of the window."""
        user32 = ctypes.windll.user32
        hwnd = int(window_handle) if window_handle else user32.GetDesktopWindow()
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return rect.left, rect.top

    def capture(self, window_handle: str | None) -> DesktopScreenshot:
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        hwnd = int(window_handle) if window_handle else user32.GetDesktopWindow()
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            raise DesktopError("capture_failed", f"invalid window rect {rect}")
        hdc_window = user32.GetWindowDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
        bitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
        gdi32.SelectObject(hdc_mem, bitmap)
        gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_window, 0, 0,
                     0x00CC0020 | 0x40000000)  # SRCCOPY | CAPTUREBLT

        class _BMIH(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_ulong), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
                        ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_ulong),
                        ("biSizeImage", ctypes.c_ulong), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_ulong),
                        ("biClrImportant", ctypes.c_ulong)]

        header = _BMIH(biSize=ctypes.sizeof(_BMIH), biWidth=width, biHeight=-height,
                       biPlanes=1, biBitCount=32, biCompression=0)
        pixels = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(hdc_mem, bitmap, 0, height, pixels,
                        ctypes.byref(header), 0)
        row_size = width * 4
        data = bytearray()
        # strip the unused alpha byte → 24-bit BGR rows padded to 4 bytes
        padded = (width * 3 + 3) & ~3
        for row in range(height):
            base = row * row_size
            line = bytearray()
            for col in range(width):
                off = base + col * 4
                # pixels is a ctypes c_char buffer: single-index gives len-1 bytes
                line.append(ord(pixels[off]))
                line.append(ord(pixels[off + 1]))
                line.append(ord(pixels[off + 2]))
            data += line + b"\x00" * (padded - width * 3)

        class _BFH(ctypes.Structure):
            _fields_ = [("bfType", ctypes.c_ushort), ("bfSize", ctypes.c_ulong),
                        ("bfReserved1", ctypes.c_ushort), ("bfReserved2", ctypes.c_ushort),
                        ("bfOffBits", ctypes.c_ulong)]

        file_header = _BFH(bfType=0x4D42,
                           bfSize=14 + ctypes.sizeof(_BMIH) + len(data),
                           bfOffBits=14 + ctypes.sizeof(_BMIH))
        payload = bytes(file_header) + bytes(header) + bytes(data)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_window)
        dpi = user32.GetDpiForWindow(hwnd) if hasattr(user32, "GetDpiForWindow") else 96
        return DesktopScreenshot(mime="image/bmp", data=payload,
                                 width=width, height=height,
                                 scale_factor=round(dpi / 96.0, 2),
                                 window_handle=str(hwnd))

    def close(self) -> None:
        self._uia = None  # type: ignore[assignment]
        if self._need_couninit:
            self.ole32.CoUninitialize()
            self._need_couninit = False


def register_default_backend() -> None:
    """Wire the real UIA backend into the desktop registry (win32 only)."""
    from oaset.computer.desktop import register_desktop_backend

    if sys.platform != "win32":
        return

    def factory():
        try:
            return UiaComBackend()
        except DesktopError:
            return None

    register_desktop_backend(factory)
