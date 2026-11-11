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


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
FRIENDLY = {"chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "opera": "Opera", "brave": "Brave",
            "explorer": "File Explorer", "code": "VS Code", "notepad": "Notepad", "winword": "Word",
            "excel": "Excel", "powerpnt": "PowerPoint", "outlook": "Outlook", "telegram": "Telegram",
            "discord": "Discord", "spotify": "Spotify", "steam": "Steam", "vlc": "VLC", "cmd": "Command Prompt",
            "windowsterminal": "Terminal", "powershell": "PowerShell", "obs64": "OBS", "slack": "Slack",
            "calculatorapp": "Calculator", "calculator": "Calculator", "systemsettings": "Settings",
            "photos": "Photos", "microsoft.media.player": "Media Player", "hxoutlook": "Mail"}
FRAME_HOST = "applicationframehost"  # Store apps' windows belong to this shared host process
_ENUM_CHILD = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumChildWindows.argtypes = [wintypes.HWND, _ENUM_CHILD, wintypes.LPARAM]
_app_names: dict[int, str] = {}


def _exe_stem(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf, n = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value.rsplit("\\", 1)[-1].rsplit(".", 1)[0]
    finally:
        kernel32.CloseHandle(h)
    return ""


def real_pid(hwnd: int) -> int:
    """The process that really owns a window. For a Store app (Calculator, Settings...) the frame
    belongs to the shared ApplicationFrameHost; the app itself owns a child window inside it."""
    pid = window_pid(hwnd)
    if _exe_stem(pid).lower() != FRAME_HOST:
        return pid
    found = []

    @_ENUM_CHILD
    def cb(child, _l):
        p = window_pid(child)
        if p != pid:
            found.append(p)
            return False
        return True
    user32.EnumChildWindows(hwnd, cb, 0)
    return found[0] if found else 0  # 0: never kill the shared host


def app_name(hwnd: int) -> str:
    """ "Chrome", "File Explorer"...: which program a window belongs to (titles alone often don't say)."""
    pid = real_pid(hwnd) or window_pid(hwnd)
    if pid in _app_names:
        return _app_names[pid]
    stem = _exe_stem(pid)
    name = FRIENDLY.get(stem.lower(), stem) if stem else ""
    if len(_app_names) > 300:
        _app_names.clear()
    _app_names[pid] = name
    return name


def default_browser() -> str | None:
    """Path of the default web browser's program (from the https association), or None."""
    import shlex
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations"
                            r"\UrlAssociations\https\UserChoice") as k:
            prog_id = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog_id + r"\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
        exe = shlex.split(cmd, posix=False)[0].strip('"')
        return exe if exe.lower().endswith(".exe") else None
    except (OSError, IndexError, ValueError):
        return None


def open_in_new_window(url: str) -> bool:
    """Open `url` in a NEW window of the default browser (Chromium browsers and Firefox
    both understand --new-window). False if the browser couldn't be found."""
    import subprocess
    exe = default_browser()
    if exe is None:
        return False
    subprocess.Popen([exe, "--new-window", url], close_fds=True)
    return True


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


# ------------------------------------------------------------ scrolling
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120


def scroll(notches: int) -> None:
    """Mouse wheel at the cursor: positive = down the page, negative = up."""
    delta = ctypes.c_int32(-int(notches) * WHEEL_DELTA).value & 0xFFFFFFFF  # DWORD holds the signed delta
    _send(INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(0, 0, delta, MOUSEEVENTF_WHEEL, 0, 0))))


# ------------------------------------------------------- window layout
SW_MAXIMIZE = 3
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
DWMWA_EXTENDED_FRAME_BOUNDS = 9

# where a window goes, as fractions (left, top, right, bottom) of the screen's work area
PLACES = {
    "left": (0, 0, .5, 1), "right": (.5, 0, 1, 1), "top": (0, 0, 1, .5), "bottom": (0, .5, 1, 1),
    "top_left": (0, 0, .5, .5), "top_right": (.5, 0, 1, .5),
    "bottom_left": (0, .5, .5, 1), "bottom_right": (.5, .5, 1, 1),
    "left_third": (0, 0, 1 / 3, 1), "middle_third": (1 / 3, 0, 2 / 3, 1), "right_third": (2 / 3, 0, 1, 1),
    "center": (.15, .1, .85, .9), "maximize": (0, 0, 1, 1),
}


def place_window(hwnd: int, place: str, work) -> bool:
    """Snap a window into `place` on a monitor work area (Rect with left/top/right/bottom).
    The visible edges land exactly on the target (Windows 10/11 windows have an invisible
    resize border that GetWindowRect includes, so it's added back)."""
    frac = PLACES.get(place)
    if frac is None or not exists(hwnd):
        return False
    if place == "maximize":
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
        return True
    if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    w, h = work.right - work.left, work.bottom - work.top
    l, t = work.left + frac[0] * w, work.top + frac[1] * h
    r, b = work.left + frac[2] * w, work.top + frac[3] * h
    outer, vis = wintypes.RECT(), wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(outer))
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(vis), ctypes.sizeof(vis)) != 0:
        vis = outer
    ml, mt = vis.left - outer.left, vis.top - outer.top
    mr, mb = outer.right - vis.right, outer.bottom - vis.bottom
    return bool(user32.SetWindowPos(hwnd, 0, int(l - ml), int(t - mt), int(r - l + ml + mr), int(b - t + mt + mb),
                                    SWP_NOZORDER | SWP_NOACTIVATE))


# ------------------------------------------------------------ shortcuts
KEYEVENTF_EXTENDEDKEY = 0x0001
VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "f5": 0x74, "f11": 0x7A,
    "media_play_pause": 0xB3, "media_next": 0xB0, "media_prev": 0xB1, "volume_mute": 0xAD,
    "volume_down": 0xAE, "volume_up": 0xAF, "+": 0xBB, "plus": 0xBB, "-": 0xBD, "minus": 0xBD,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
}
MODIFIERS = {"ctrl", "control", "alt", "shift", "win"}
EXTENDED = {0x2E, 0x2D, 0x26, 0x28, 0x25, 0x27, 0x24, 0x23, 0x21, 0x22, 0x5B}
BLOCKED = ({"ctrl", "alt", "delete"}, {"win", "l"}, {"win", "r"})  # system-level: never pressed for you


def parse_keys(text: str) -> list[list[str]] | None:
    """ "ctrl+t, ctrl+l enter" -> [["ctrl","t"], ["ctrl","l"], ["enter"]]; None if anything is unknown."""
    combos = []
    for chunk in text.replace(",", " ").split():
        parts = [p.strip().lower() for p in chunk.split("+") if p.strip()] or (["+"] if "+" in chunk else [])
        if not parts or any(p not in VK for p in parts) or sum(p not in MODIFIERS for p in parts) != 1:
            return None
        if any(set(parts) >= b for b in BLOCKED):
            return None
        combos.append(parts)
    return combos[:8] or None


def press_combo(parts: list[str]) -> None:
    """Hold the modifiers, tap the key, release in reverse order."""
    codes = [VK[p] for p in parts]
    flag = lambda vk: KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0  # noqa: E731
    _send(*[_key(vk, flag(vk)) for vk in codes],
          *[_key(vk, flag(vk) | KEYEVENTF_KEYUP) for vk in reversed(codes)])
