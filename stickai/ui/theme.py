"""One look for every StickAI window: dark title bars, dark menus, shared colours."""
from __future__ import annotations

import ctypes
import sys

BG = "#1d2026"
SURFACE = "#252932"
FIELD = "#2a2e36"
BORDER = "#3a3f4a"
TEXT = "#e9e9ea"
MUTED = "#8d929c"
ACCENT = "#FEC503"
ACCENT_HOVER = "#ffd43b"
DANGER = "#ff8a80"
DANGER_BG = "#5a2a2a"

MENU_STYLE = f"""
QMenu {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 8px; padding: 5px;
         font: 10pt 'Segoe UI'; }}
QMenu::item {{ padding: 6px 22px 6px 26px; border-radius: 6px; }}
QMenu::item:selected {{ background: #3b4150; color: #ffffff; }}
QMenu::item:disabled {{ color: {MUTED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QMenu::indicator {{ width: 12px; height: 12px; left: 8px; }}
QMenu::indicator:checked {{ background: {ACCENT}; border-radius: 3px; }}
QMenu::indicator:unchecked {{ border: 1px solid {BORDER}; border-radius: 3px; }}
"""

TITLE_BAR = 34   # logical px a window's title bar + frame add around its client area (approx.)


def _screen_geo(widget):
    from PySide6.QtGui import QCursor, QGuiApplication
    scr = widget.screen() if widget.isVisible() else (QGuiApplication.screenAt(QCursor.pos())
                                                       or QGuiApplication.primaryScreen())
    return scr.availableGeometry()


def fit_window(widget, w: int, h: int, min_w: int = 320, min_h: int = 300) -> None:
    """Resize to w x h, but never bigger than the screen it opens on (laptops: nothing off-screen)."""
    geo = _screen_geo(widget)
    max_w, max_h = int(geo.width() * 0.96), int(geo.height() - TITLE_BAR - 16)
    widget.setMinimumSize(min(min_w, max_w), min(min_h, max_h))
    widget.resize(min(w, max_w), min(h, max_h))


def keep_on_screen(widget) -> None:
    """Shrink / move a window back inside the available area of its screen (title bar included)."""
    geo = _screen_geo(widget)
    max_w, max_h = geo.width() - 16, geo.height() - TITLE_BAR - 8
    if widget.width() > max_w or widget.height() > max_h:
        widget.resize(min(widget.width(), max_w), min(widget.height(), max_h))
    fr = widget.frameGeometry()
    extra_w, extra_h = (fr.width() - widget.width(), fr.height() - widget.height()) if widget.isVisible()         else (2, TITLE_BAR)
    fw, fh = widget.width() + extra_w, widget.height() + extra_h
    x = min(max(fr.x(), geo.left() + 4), geo.right() - fw - 4)
    y = min(max(fr.y(), geo.top() + 4), geo.bottom() - fh - 4)
    if (x, y) != (fr.x(), fr.y()):
        widget.move(x, y)


_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36


def _colorref(hex_color: str) -> int:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r | (g << 8) | (b << 16)


def dark_title_bar(widget) -> None:
    """Give a top-level window Windows' dark title bar, tinted to our background,
    so it matches the dark contents instead of the default white caption.
    Harmless no-op on older Windows / other platforms."""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(on), ctypes.sizeof(on))
        cap = ctypes.c_uint(_colorref(BG))     # Windows 11 only; ignored elsewhere
        dwm.DwmSetWindowAttribute(hwnd, _DWMWA_CAPTION_COLOR, ctypes.byref(cap), ctypes.sizeof(cap))
        txt = ctypes.c_uint(_colorref("#e1e2e5"))
        dwm.DwmSetWindowAttribute(hwnd, _DWMWA_TEXT_COLOR, ctypes.byref(txt), ctypes.sizeof(txt))
    except Exception:  # noqa: BLE001 - cosmetic only
        pass
