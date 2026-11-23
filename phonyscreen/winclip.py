"""Minimal Windows clipboard access via the Win32 API: Unicode text, and pictures (CF_DIB plus the
"PNG" format browsers and Office use; converted with Pillow)."""

from __future__ import annotations

import io
import sys
import time
from typing import Optional

CF_UNICODETEXT = 13
CF_DIB = 8
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002
MAX_IMAGE_PIXELS = 50_000_000  # don't try to shuttle anything bigger (a 12 MP photo is 12 M)


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
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
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


def _global(ctypes, kernel32, data: bytes):
    """Movable global memory holding ``data`` (what SetClipboardData wants), or None."""
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return None
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return None
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)
    return handle


def _set(formats: list[tuple[int, bytes]], hwnd: Optional[int]) -> bool:
    """Replace the clipboard with ``formats`` [(format, bytes)]. Pass our window handle: after
    EmptyClipboard on a clipboard opened without an owner window, SetClipboardData fails."""
    if sys.platform != "win32":
        return False
    ctypes, user32, kernel32 = _api()
    handles = [(fmt, _global(ctypes, kernel32, data)) for fmt, data in formats]
    if any(h is None for _f, h in handles) or not _open(user32, hwnd):
        for _f, h in handles:
            if h:
                kernel32.GlobalFree(h)
        return False
    ok = True
    try:
        user32.EmptyClipboard()
        for fmt, h in handles:
            if not user32.SetClipboardData(fmt, h):
                kernel32.GlobalFree(h)  # still ours on failure; on success the clipboard owns it
                ok = False
        return ok
    finally:
        user32.CloseClipboard()


def set_text(text: str, hwnd: Optional[int] = None) -> bool:
    """Put text on the clipboard."""
    return _set([(CF_UNICODETEXT, text.encode("utf-16-le") + b"\x00\x00")], hwnd)


def sequence_number() -> int:
    """Windows' clipboard change counter: compare to tell whether the clipboard changed."""
    if sys.platform != "win32":
        return 0
    return int(_api()[1].GetClipboardSequenceNumber())


def _png_format(user32) -> int:
    return int(user32.RegisterClipboardFormatW("PNG"))


def image_formats(data: bytes) -> Optional[list[tuple[int, bytes]]]:
    """Clipboard formats for a picture (PNG/JPEG/... bytes): CF_DIB, which every app can paste,
    plus "PNG", which keeps transparency in apps that read it. Slow for big photos (decode and
    PNG encode), and doesn't touch the clipboard: call it off the window thread."""
    if sys.platform != "win32":
        return None
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None  # e.g. HEIC, which Pillow can't decode
    if img.width * img.height > MAX_IMAGE_PIXELS:
        return None
    bmp = io.BytesIO()
    img.convert("RGB").save(bmp, format="BMP")
    png = io.BytesIO()
    img.save(png, format="PNG")
    # CF_DIB = a BMP file without its 14-byte file header
    return [(CF_DIB, bmp.getvalue()[14:]), (_png_format(_api()[1]), png.getvalue())]


def set_formats(formats: list[tuple[int, bytes]], hwnd: Optional[int] = None) -> bool:
    """Replace the clipboard with ready formats (from image_formats)."""
    return _set(formats, hwnd)


def set_image(data: bytes, hwnd: Optional[int] = None) -> bool:
    formats = image_formats(data)
    return bool(formats) and _set(formats, hwnd)  # type: ignore[arg-type]


def has_image() -> bool:
    """Cheap check: does the clipboard hold a picture (no reading, no converting)?"""
    if sys.platform != "win32":
        return False
    user32 = _api()[1]
    return any(user32.IsClipboardFormatAvailable(f) for f in (_png_format(user32), CF_DIBV5, CF_DIB))


def _read_format(ctypes, user32, kernel32, fmt: int) -> Optional[bytes]:
    handle = user32.GetClipboardData(fmt)
    if not handle:
        return None
    size = kernel32.GlobalSize(handle)
    ptr = kernel32.GlobalLock(handle)
    if not ptr or not size:
        return None
    try:
        return ctypes.string_at(ptr, size)
    finally:
        kernel32.GlobalUnlock(handle)


def get_image_png() -> Optional[bytes]:
    """The clipboard's picture as PNG bytes (from "PNG" if there, else from the bitmap), or None."""
    if sys.platform != "win32":
        return None
    ctypes, user32, kernel32 = _api()
    png_fmt = _png_format(user32)
    if not any(user32.IsClipboardFormatAvailable(f) for f in (png_fmt, CF_DIBV5, CF_DIB)):
        return None
    if not _open(user32):
        return None
    try:
        png = _read_format(ctypes, user32, kernel32, png_fmt) if user32.IsClipboardFormatAvailable(png_fmt) else None
        dib = None if png else _read_format(ctypes, user32, kernel32, CF_DIB)
    finally:
        user32.CloseClipboard()
    from PIL import Image

    try:
        if png:
            img = Image.open(io.BytesIO(png))
        elif dib:
            img = Image.open(io.BytesIO(_bmp_file_header(dib) + dib))
        else:
            return None
        img.load()
        if img.width * img.height > MAX_IMAGE_PIXELS:
            return None
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return None


def _bmp_file_header(dib: bytes) -> bytes:
    """The 14-byte BMP file header that turns a CF_DIB into a .bmp file Pillow can open."""
    import struct

    header_size, = struct.unpack_from("<I", dib, 0)
    bit_count, = struct.unpack_from("<H", dib, 14)
    compression, = struct.unpack_from("<I", dib, 16)
    colors_used, = struct.unpack_from("<I", dib, 32)
    masks = 12 if compression == 3 and header_size == 40 else 0  # BI_BITFIELDS after a BITMAPINFOHEADER
    palette = (colors_used or (1 << bit_count if bit_count <= 8 else 0)) * 4
    offset = 14 + header_size + masks + palette
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, offset)
