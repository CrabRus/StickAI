"""Thin ctypes wrapper over the Win32 / DWM APIs StickAI needs.

Everything here works in *physical pixels* of the Windows virtual desktop.
That only holds while the process is Per-Monitor-DPI-Aware (v2); Qt 6 turns
that on when QApplication is created, and `dpi_awareness()` lets us verify it.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
winmm = ctypes.WinDLL("winmm")

# --- constants -------------------------------------------------------------
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14

MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 1

HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4 & 0xFFFFFFFFFFFFFFFF)

# Top-level windows that are never "furniture" for the stickmen.
IGNORED_CLASSES = frozenset({
    "Progman", "WorkerW",                       # desktop wallpaper / icons
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd",  # taskbars (we use the work area instead)
    "Windows.UI.Core.CoreWindow",               # Start menu, search, notification flyouts
    "XamlExplorerHostIslandWindow",
})

# --- prototypes ------------------------------------------------------------
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT
]
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM]
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long  # HRESULT


# --- data types ------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x < self.right and self.top <= y <= self.bottom

    def intersects(self, o: "Rect") -> bool:
        return self.left < o.right and o.left < self.right and self.top < o.bottom and o.top < self.bottom

    def union(self, o: "Rect") -> "Rect":
        return Rect(min(self.left, o.left), min(self.top, o.top), max(self.right, o.right), max(self.bottom, o.bottom))

    @staticmethod
    def from_win(r: wintypes.RECT) -> "Rect":
        return Rect(r.left, r.top, r.right, r.bottom)


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    rect: Rect  # visible frame (no invisible resize border)


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    device: str
    bounds: Rect
    work: Rect  # bounds minus taskbar(s)
    primary: bool


# --- helpers ---------------------------------------------------------------
def _text(fn, hwnd, size: int) -> str:
    buf = ctypes.create_unicode_buffer(size)
    fn(hwnd, buf, size)
    return buf.value


def is_cloaked(hwnd) -> bool:
    """UWP apps that are suspended, windows on other virtual desktops, etc."""
    cloaked = wintypes.DWORD(0)
    hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return hr == 0 and cloaked.value != 0


def frame_bounds(hwnd) -> Rect | None:
    """The window's *visible* rectangle.

    GetWindowRect includes the ~7px invisible resize border Windows 10/11 adds
    around normal windows; standing on that would make the stickman float.
    DWMWA_EXTENDED_FRAME_BOUNDS is the rectangle you actually see.
    """
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
    return Rect.from_win(r)


def window_rect(hwnd) -> Rect | None:
    """Raw GetWindowRect (used for our own overlays, which have no frame)."""
    r = wintypes.RECT()
    return Rect.from_win(r) if user32.GetWindowRect(hwnd, ctypes.byref(r)) else None


def query_live_rect(hwnd: int) -> Rect | None:
    """Fast per-frame query for the window a stickman is standing on.

    Returns None when the window is gone, hidden, minimized or cloaked.
    """
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return None
    if is_cloaked(hwnd):
        return None
    return frame_bounds(hwnd)


def _describe(hwnd, exclude_pid: int) -> WindowInfo | None:
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return None
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    # Tool windows (tooltips, menus, palettes) and click-through overlays
    # (GPU/recording overlays, other desktop pets) are not solid furniture.
    if ex & WS_EX_TOOLWINDOW or (ex & WS_EX_TRANSPARENT and ex & WS_EX_LAYERED):
        return None
    if is_cloaked(hwnd):
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == exclude_pid:
        return None
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return None
    cls = _text(user32.GetClassNameW, hwnd, 256)
    if cls in IGNORED_CLASSES:
        return None
    rect = frame_bounds(hwnd)
    if rect is None or rect.width < 8 or rect.height < 8:
        return None
    return WindowInfo(int(hwnd), _text(user32.GetWindowTextW, hwnd, n + 1), cls, rect)


def enumerate_windows(exclude_pid: int) -> list[WindowInfo]:
    """Visible top-level windows in Z-order, topmost first."""
    found: list[WindowInfo] = []

    @WNDENUMPROC
    def callback(hwnd, _lparam):
        try:
            info = _describe(hwnd, exclude_pid)
            if info is not None:
                found.append(info)
        except OSError:
            pass  # window died mid-query
        return True

    user32.EnumWindows(callback, 0)
    return found


def enumerate_monitors() -> list[MonitorInfo]:
    found: list[MonitorInfo] = []

    @MONITORENUMPROC
    def callback(hmon, _hdc, _rect, _lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            found.append(MonitorInfo(
                mi.szDevice, Rect.from_win(mi.rcMonitor), Rect.from_win(mi.rcWork),
                bool(mi.dwFlags & MONITORINFOF_PRIMARY),
            ))
        return True

    user32.EnumDisplayMonitors(None, None, callback, 0)
    return found


def monitor_for_window(hwnd: int) -> MonitorInfo | None:
    hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFOEXW()
    mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
    if not hmon or not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    return MonitorInfo(mi.szDevice, Rect.from_win(mi.rcMonitor), Rect.from_win(mi.rcWork),
                       bool(mi.dwFlags & MONITORINFOF_PRIMARY))


def foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0)


def make_click_through(hwnd: int) -> bool:
    """Make mouse input fall through the overlay and stop it ever taking focus.

    Returns whether the window is layered (Qt makes translucent windows
    layered), which click-through requires.
    """
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
    return bool(ex & WS_EX_LAYERED)


def set_no_activate(hwnd: int) -> None:
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE)


def set_click_through(hwnd: int, enabled: bool) -> None:
    """Toggle WS_EX_TRANSPARENT. Off = the overlay receives mouse input, but
    only on pixels with alpha > 0 (Windows hit-tests layered windows per pixel)."""
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    new = ex | WS_EX_TRANSPARENT if enabled else ex & ~WS_EX_TRANSPARENT
    if new != ex:
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new)


user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
VK_LBUTTON = 0x01


def cursor_pos() -> tuple[int, int]:
    """Cursor in physical virtual-desktop pixels (we are Per-Monitor-v2 aware)."""
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def left_button_down() -> bool:
    return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def window_from_point(x: int, y: int) -> int:
    return int(user32.WindowFromPoint(wintypes.POINT(x, y)) or 0)


def keep_topmost(hwnd: int) -> None:
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def is_per_monitor_v2() -> bool:
    ctx = user32.GetThreadDpiAwarenessContext()
    return bool(user32.AreDpiAwarenessContextsEqual(ctx, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))


def set_timer_resolution(enable: bool) -> None:
    """1 ms scheduler granularity so a 16 ms frame timer doesn't drift to 31 ms."""
    (winmm.timeBeginPeriod if enable else winmm.timeEndPeriod)(1)
