"""Raw touchpad contacts (Windows Precision Touchpad) for the gesture lab.

Two-finger scrolling reaches the app only as wheel events, and Windows keeps sending
wheel events after the fingers lift (its own inertia), so the wheel stream alone can't
say when the fingers left the pad. This reads the touchpad's HID reports through Raw
Input on a hidden message-only window (its own thread, RIDEV_INPUTSINK so it doesn't
need focus) and calls ``on_report(contacts, count)`` per report, with contacts as
``(contact id, tip down, x mm, y mm)``.

Dev-only: nothing here runs in a normal session. Every failure leaves it off.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger("phonyscreen.padraw")

WM_INPUT = 0x00FF
WM_CLOSE = 0x0010
RID_INPUT = 0x10000003
RIDI_PREPARSEDDATA = 0x20000005
RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001
RIM_TYPEHID = 2
HWND_MESSAGE = -3
HIDP_STATUS_SUCCESS = 0x00110000
HID_PAGE_DESKTOP, HID_PAGE_DIGITIZER = 0x01, 0x0D
USAGE_X, USAGE_Y = 0x30, 0x31
USAGE_TOUCHPAD, USAGE_TIP, USAGE_CONTACT_ID, USAGE_CONTACT_COUNT = 0x05, 0x42, 0x51, 0x54

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT), ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD), ("hDevice", wt.HANDLE), ("wParam", wt.WPARAM)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT), ("InputReportByteLength", wt.USHORT),
                ("OutputReportByteLength", wt.USHORT), ("FeatureReportByteLength", wt.USHORT),
                ("Reserved", wt.USHORT * 17), ("NumberLinkCollectionNodes", wt.USHORT),
                ("NumberInputButtonCaps", wt.USHORT), ("NumberInputValueCaps", wt.USHORT),
                ("NumberInputDataIndices", wt.USHORT), ("NumberOutputButtonCaps", wt.USHORT),
                ("NumberOutputValueCaps", wt.USHORT), ("NumberOutputDataIndices", wt.USHORT),
                ("NumberFeatureButtonCaps", wt.USHORT), ("NumberFeatureValueCaps", wt.USHORT),
                ("NumberFeatureDataIndices", wt.USHORT)]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _fields_ = [("UsagePage", wt.USHORT), ("ReportID", ctypes.c_ubyte), ("IsAlias", ctypes.c_ubyte),
                ("BitField", wt.USHORT), ("LinkCollection", wt.USHORT), ("LinkUsage", wt.USHORT),
                ("LinkUsagePage", wt.USHORT), ("IsRange", ctypes.c_ubyte), ("IsStringRange", ctypes.c_ubyte),
                ("IsDesignatorRange", ctypes.c_ubyte), ("IsAbsolute", ctypes.c_ubyte), ("HasNull", ctypes.c_ubyte),
                ("Reserved", ctypes.c_ubyte), ("BitSize", wt.USHORT), ("ReportCount", wt.USHORT),
                ("Reserved2", wt.USHORT * 5), ("UnitsExp", wt.ULONG), ("Units", wt.ULONG),
                ("LogicalMin", wt.LONG), ("LogicalMax", wt.LONG), ("PhysicalMin", wt.LONG), ("PhysicalMax", wt.LONG),
                ("Usage", wt.USHORT), ("UsageMax", wt.USHORT), ("_rest", wt.USHORT * 6)]


assert ctypes.sizeof(HIDP_CAPS) == 64 and ctypes.sizeof(HIDP_VALUE_CAPS) == 72


def _mm_per_unit(vc: HIDP_VALUE_CAPS) -> Optional[float]:
    """Millimetres per logical unit from the report descriptor's physical extent and unit."""
    span = vc.LogicalMax - vc.LogicalMin
    phys = vc.PhysicalMax - vc.PhysicalMin
    if span <= 0 or phys <= 0:
        return None
    exp = vc.UnitsExp & 0xF
    exp = exp - 16 if exp >= 8 else exp
    system, length = vc.Units & 0xF, (vc.Units >> 4) & 0xF
    if length != 1:
        return None
    per = {1: 10.0, 3: 25.4}.get(system)  # SI linear: cm; English linear: inch
    if per is None:
        return None
    return phys * (10.0 ** exp) * per / span


class _Device:
    """One touchpad's report layout: which link collections are fingers, and their scale."""

    def __init__(self, preparsed: ctypes.Array, hid: ctypes.WinDLL) -> None:
        self.preparsed = preparsed
        self.hid = hid
        caps = HIDP_CAPS()
        if hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
            raise OSError("HidP_GetCaps failed")
        n = wt.USHORT(caps.NumberInputValueCaps)
        vcs = (HIDP_VALUE_CAPS * max(1, n.value))()
        if hid.HidP_GetValueCaps(0, vcs, ctypes.byref(n), preparsed) != HIDP_STATUS_SUCCESS:
            raise OSError("HidP_GetValueCaps failed")
        self.fingers: dict[int, list[Optional[float]]] = {}  # link collection -> [mm/unit x, mm/unit y, min x, min y]
        self.size_mm = [0.0, 0.0]
        for vc in vcs[: n.value]:
            if vc.UsagePage == HID_PAGE_DESKTOP and vc.Usage in (USAGE_X, USAGE_Y) and not vc.IsRange:
                f = self.fingers.setdefault(vc.LinkCollection, [None, None, 0, 0])
                i = 0 if vc.Usage == USAGE_X else 1
                f[i], f[2 + i] = _mm_per_unit(vc), vc.LogicalMin
                if f[i]:
                    self.size_mm[i] = max(self.size_mm[i], (vc.LogicalMax - vc.LogicalMin) * f[i])
        if not self.fingers:
            raise OSError("no finger collections")

    def _value(self, page: int, link: int, usage: int, report: ctypes.Array, n: int) -> Optional[int]:
        v = wt.ULONG()
        ok = self.hid.HidP_GetUsageValue(0, page, link, usage, ctypes.byref(v), self.preparsed, report, n)
        return v.value if ok == HIDP_STATUS_SUCCESS else None

    def _tip(self, link: int, report: ctypes.Array, n: int) -> bool:
        usages = (wt.USHORT * 16)()
        count = wt.ULONG(16)
        ok = self.hid.HidP_GetUsages(0, HID_PAGE_DIGITIZER, link, usages, ctypes.byref(count), self.preparsed, report, n)
        return ok == HIDP_STATUS_SUCCESS and USAGE_TIP in usages[: count.value]

    def parse(self, report: ctypes.Array, n: int) -> tuple[list[tuple[int, bool, float, float]], int]:
        contacts = []
        for link, (sx, sy, mx, my) in self.fingers.items():
            x = self._value(HID_PAGE_DESKTOP, link, USAGE_X, report, n)
            y = self._value(HID_PAGE_DESKTOP, link, USAGE_Y, report, n)
            if x is None or y is None:
                continue  # not in this report (hybrid reporting splits fingers over reports)
            cid = self._value(HID_PAGE_DIGITIZER, link, USAGE_CONTACT_ID, report, n)
            contacts.append((cid if cid is not None else link, self._tip(link, report, n),
                             round((x - mx) * (sx or 1.0), 2), round((y - my) * (sy or 1.0), 2)))
        count = self._value(HID_PAGE_DIGITIZER, 0, USAGE_CONTACT_COUNT, report, n)
        return contacts, (count if count is not None else -1)


class TouchpadReader:
    """Background Raw Input reader. ``on_report(device index, contacts, contact count, size_mm)``."""

    def __init__(self, on_report: Callable[[int, list, int, list], None]) -> None:
        self.on_report = on_report
        self.hwnd: Optional[int] = None
        self.ok = False
        self.devices: dict[int, _Device] = {}
        self._failed: set[int] = set()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="touchpad-raw")

    def start(self, timeout: float = 2.0) -> bool:
        self._thread.start()
        self._ready.wait(timeout)
        return self.ok

    def stop(self) -> None:
        if self.hwnd:
            post = ctypes.WinDLL("user32").PostMessageW
            post.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
            post(self.hwnd, WM_CLOSE, 0, 0)

    def _device(self, handle: int) -> Optional[_Device]:
        dev = self.devices.get(handle)
        if dev is not None or handle in self._failed:
            return dev
        user32 = ctypes.windll.user32
        size = wt.UINT(0)
        user32.GetRawInputDeviceInfoW(wt.HANDLE(handle), RIDI_PREPARSEDDATA, None, ctypes.byref(size))
        buf = (ctypes.c_ubyte * max(1, size.value))()
        try:
            if user32.GetRawInputDeviceInfoW(wt.HANDLE(handle), RIDI_PREPARSEDDATA, buf, ctypes.byref(size)) <= 0:
                raise OSError("no preparsed data")
            dev = _Device(buf, ctypes.WinDLL("hid"))
        except Exception as e:
            logger.info("touchpad device not readable: %s", e)
            self._failed.add(handle)
            return None
        self.devices[handle] = dev
        logger.info("touchpad: %d finger slots, %.0f x %.0f mm", len(dev.fingers), *dev.size_mm)
        return dev

    def _on_input(self, lparam: int) -> None:
        user32 = ctypes.windll.user32
        size = wt.UINT(0)
        hdr = ctypes.sizeof(RAWINPUTHEADER)
        user32.GetRawInputData(wt.HANDLE(lparam), RID_INPUT, None, ctypes.byref(size), hdr)
        buf = (ctypes.c_ubyte * size.value)()
        if user32.GetRawInputData(wt.HANDLE(lparam), RID_INPUT, buf, ctypes.byref(size), hdr) != size.value:
            return
        head = RAWINPUTHEADER.from_buffer(buf)
        if head.dwType != RIM_TYPEHID or not head.hDevice:
            return
        dev = self._device(head.hDevice)
        if dev is None:
            return
        each, count = wt.DWORD.from_buffer(buf, hdr).value, wt.DWORD.from_buffer(buf, hdr + 4).value
        for i in range(count):
            off = hdr + 8 + i * each
            report = (ctypes.c_char * each).from_buffer(buf, off)
            contacts, n = dev.parse(report, each)
            self.on_report(list(self.devices).index(head.hDevice), contacts, n, dev.size_mm)

    def _run(self) -> None:
        # private DLL handles: the prototypes set here don't leak into other ctypes users
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.restype = wt.HMODULE  # 64-bit: the default int return truncates it
        kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.CreateWindowExW.restype = wt.HWND
        user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
        user32.DestroyWindow.argtypes = [wt.HWND]
        user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
        user32.DispatchMessageW.restype = LRESULT

        def proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_INPUT:
                try:
                    self._on_input(lparam)
                except Exception:
                    logger.exception("touchpad report")
            elif msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(proc)  # keep a reference: Windows calls it
        try:
            class WNDCLASSW(ctypes.Structure):
                _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                            ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                            ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                            ("lpszClassName", wt.LPCWSTR)]

            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName="PhonyScreenTouchpadRaw")
            user32.RegisterClassW(ctypes.byref(wc))
            hwnd = user32.CreateWindowExW(0, "PhonyScreenTouchpadRaw", None, 0, 0, 0, 0, 0,
                                          wt.HWND(HWND_MESSAGE), None, hinst, None)
            if not hwnd:
                raise OSError(f"CreateWindowEx failed ({ctypes.get_last_error()})")
            rid = RAWINPUTDEVICE(HID_PAGE_DIGITIZER, USAGE_TOUCHPAD, RIDEV_INPUTSINK, hwnd)
            if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)):
                raise OSError(f"RegisterRawInputDevices failed ({ctypes.get_last_error()})")
            self.hwnd, self.ok = hwnd, True
        except Exception as e:
            logger.info("touchpad raw input off: %s", e)
            self._ready.set()
            return
        self._ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
