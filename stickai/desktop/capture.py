"""Thread-safe screen capture with plain GDI (no Qt), straight into numpy.

* runs on any thread (the game's target finder runs in the background, so
  grabbing a 4K page never freezes the animation or the windows);
* scales down while copying (StretchBlt), so a 4K area costs a fraction;
* note: with desktop composition the copy DOES include layered windows (our
  stickmen and bubbles), so callers must mask those out themselves.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

import numpy as np

user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")

SRCCOPY = 0x00CC0020
COLORONCOLOR = 3
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


def grab(left: int, top: int, width: int, height: int, scale: float = 1.0) -> np.ndarray | None:
    """Physical-pixel screen rectangle -> HxWx3 RGB uint8 array (scaled), or None."""
    dw, dh = max(1, int(width * scale)), max(1, int(height * scale))
    screen = user32.GetDC(None)
    if not screen:
        return None
    mem = gdi32.CreateCompatibleDC(screen)
    bmi = BITMAPINFO()
    h = bmi.bmiHeader
    h.biSize, h.biWidth, h.biHeight = ctypes.sizeof(BITMAPINFOHEADER), dw, -dh  # negative: top-down rows
    h.biPlanes, h.biBitCount, h.biCompression = 1, 32, 0
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
    try:
        if not bmp or not bits.value:
            return None
        old = gdi32.SelectObject(mem, bmp)
        gdi32.SetStretchBltMode(mem, COLORONCOLOR)
        ok = gdi32.StretchBlt(mem, 0, 0, dw, dh, screen, int(left), int(top), int(width), int(height), SRCCOPY)
        gdi32.SelectObject(mem, old)
        if not ok:
            return None
        buf = (ctypes.c_uint8 * (dw * dh * 4)).from_address(bits.value)
        return np.frombuffer(buf, dtype=np.uint8).reshape(dh, dw, 4)[:, :, 2::-1].copy()  # BGRA -> RGB
    finally:
        if bmp:
            gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
