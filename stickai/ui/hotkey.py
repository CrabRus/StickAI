"""System-wide hotkey via Win32 RegisterHotKey.

Windows posts WM_HOTKEY to our thread's message queue; Qt's event
dispatcher hands every message to native event filters first, so a filter
picks it up without any extra window or thread.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QTimer

log = logging.getLogger("stickai.hotkey")
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]

WM_HOTKEY = 0x0312
MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
KEYS = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "`": 0xC0, "/": 0xBF, ";": 0xBA,
        "'": 0xDE, "[": 0xDB, "]": 0xDD, "-": 0xBD, "=": 0xBB, ",": 0xBC, ".": 0xBE}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+space' -> (modifiers, virtual-key). Raises ValueError."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    mods = 0
    for p in parts[:-1]:
        if p not in MODS:
            raise ValueError(f"unknown modifier {p!r} in hotkey {spec!r}")
        mods |= MODS[p]
    k = parts[-1]
    if len(k) == 1 and k.isalnum():
        vk = ord(k.upper())
    elif k in KEYS:
        vk = KEYS[k]
    elif k.startswith("f") and k[1:].isdigit() and 1 <= int(k[1:]) <= 24:
        vk = 0x6F + int(k[1:])
    else:
        raise ValueError(f"unknown key {k!r} in hotkey {spec!r}")
    return mods, vk


class GlobalHotkey(QAbstractNativeEventFilter):
    def __init__(self, callback: Callable[[], None], hotkey_id: int = 0x5A1):
        super().__init__()
        self.callback = callback
        self.hotkey_id = hotkey_id  # unique per hotkey in this process
        self.spec = ""
        self.registered = False

    def register(self, spec: str) -> str | None:
        """(Re)register; returns an error message or None."""
        self.unregister()
        try:
            mods, vk = parse_hotkey(spec)
        except ValueError as e:
            return str(e)
        if not user32.RegisterHotKey(None, self.hotkey_id, mods | MOD_NOREPEAT, vk):
            return f"hotkey {spec} is already used by another app; pick another in Settings"
        self.spec, self.registered = spec, True
        log.info("Hotkey %s registered", spec)
        return None

    def unregister(self) -> None:
        if self.registered:
            user32.UnregisterHotKey(None, self.hotkey_id)
            self.registered = False

    def nativeEventFilter(self, event_type, message):
        if self.registered:
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                QTimer.singleShot(0, self.callback)  # don't do UI work inside the filter
                return True, 0
        return False, 0
