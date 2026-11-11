"""Win32 helpers for the (permission-gated) things stickmen may do to your
PC: close / minimize / restore windows, move the cursor, click, type.

Nothing here decides anything: brain/tasks.py only calls these after you
pressed "Allow" for that exact action.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

WM_CLOSE = 0x0010
SW_MINIMIZE, SW_RESTORE = 6, 9
GA_ROOT = 2
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
VK_MENU = 0x12
GWL_EXSTYLE, WS_EX_TOOLWINDOW = -20, 0x80
DWMWA_CLOAKED = 14

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


# ------------------------------------------------------------- windows
def exists(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def close_window(hwnd: int) -> None:
    """Polite close, exactly like the title-bar X (the app may ask to save)."""
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def minimize_window(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_MINIMIZE)


def restore_window(hwnd: int) -> None:
    """Open / show: un-minimize if minimized (a maximized window stays maximized
    because SW_RESTORE on a minimized window returns it to its previous state),
    then bring it to the front."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # Windows only lets the foreground app hand focus around; a tap of Alt
    # (no visible effect) lifts that lock so the window really comes forward.
    _send(_key(VK_MENU, 0), _key(VK_MENU, KEYEVENTF_KEYUP))
    user32.SetForegroundWindow(hwnd)


def foreground_root() -> int:
    fg = user32.GetForegroundWindow()
    return int(user32.GetAncestor(fg, GA_ROOT) or 0) if fg else 0


user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND


def root_at(x: float, y: float) -> int:
    """Top-level window that would receive a click at (x, y)."""
    h = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return int(user32.GetAncestor(h, GA_ROOT) or 0) if h else 0


def caption_buttons(hwnd: int, rect) -> dict[str, tuple[int, int]]:
    """Approximate centres of the title-bar buttons (standard 46x30 DIP layout)."""
    s = (user32.GetDpiForWindow(hwnd) or 96) / 96.0
    y = int(rect.top + 15 * s)
    return {"close": (int(rect.right - 23 * s), y), "minimize": (int(rect.right - 115 * s), y)}


def minimized_windows(exclude_pid: int) -> list[tuple[int, str]]:
    """(hwnd, title) of minimized app windows, for "open my ... window"."""
    found: list[tuple[int, str]] = []

    @WNDENUMPROC
    def cb(hwnd, _l):
        try:
            if not user32.IsWindowVisible(hwnd) or not user32.IsIconic(hwnd):
                return True
            if user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            cloaked = wintypes.DWORD(0)
            dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), 4)
            if cloaked.value:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            n = user32.GetWindowTextLengthW(hwnd)
            if pid.value == exclude_pid or n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            found.append((int(hwnd), buf.value))
        except OSError:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return found


# ------------------------------------------------------- mouse / keyboard
def set_cursor(x: float, y: float) -> None:
    user32.SetCursorPos(int(x), int(y))


def click() -> None:
    m = lambda f: INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(0, 0, 0, f, 0, 0)))
    _send(m(MOUSEEVENTF_LEFTDOWN), m(MOUSEEVENTF_LEFTUP))


def _key(vk: int, flags: int, scan: int = 0) -> INPUT:
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, scan, flags, 0, 0)))


def type_char(ch: str) -> None:
    """Type one character as Unicode input (works for any language/layout)."""
    data = ch.encode("utf-16-le")
    for i in range(0, len(data), 2):
        unit = int.from_bytes(data[i:i + 2], "little")
        _send(_key(0, KEYEVENTF_UNICODE, unit), _key(0, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, unit))
