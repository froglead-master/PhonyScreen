"""Minimal Windows clipboard access (Unicode text) via the Win32 API, no extra dependencies."""

from __future__ import annotations

import sys
import time
from typing import Optional

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _api():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    return ctypes, user32, kernel32


def _open(user32, hwnd: Optional[int] = None) -> bool:
    for _ in range(10):  # another app may hold the clipboard for a moment
        if user32.OpenClipboard(hwnd):
            return True
        time.sleep(0.02)
    return False


def get_text() -> Optional[str]:
    if sys.platform != "win32":
        return None
    ctypes, user32, kernel32 = _api()
    if not _open(user32):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_text(text: str, hwnd: Optional[int] = None) -> bool:
    """Put text on the clipboard. Pass our window handle: after EmptyClipboard on a clipboard
    opened without an owner window, Windows makes SetClipboardData fail."""
    if sys.platform != "win32":
        return False
    ctypes, user32, kernel32 = _api()
    data = text.encode("utf-16-le") + b"\x00\x00"
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)
    if not _open(user32, hwnd):
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)  # still ours on failure
            return False
        return True  # the clipboard owns the memory now
    finally:
        user32.CloseClipboard()
