"""One-off Windows UI Automation queries used by tasks (run them on a worker thread).

* find_clickables(hwnd, name): buttons / links / tabs / menu items whose
  accessible name matches, best match first ("press Save", "click Next").
* read_field(x, y) / write_field(x, y, text): the text field at a screen point,
  through its Value pattern; word_spots(x, y, words): where words of it are on screen.
  Used only by "check the spelling in that field", which is a separate opt-in setting.
* focused_field(): the field you're typing in, for the spelling watch (another opt-in).
Password fields are never read.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("stickai.uia")
_local = threading.local()


def _auto():
    if not hasattr(_local, "auto"):
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
        _local.UIA = UIA
        _local.auto = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
    return _local.UIA, _local.auto


def _norm(s: str) -> str:
    return " ".join(s.replace("&", "").replace("…", "").replace("...", "").lower().split())


def find_clickables(hwnd: int, name: str, limit: int = 300) -> list[tuple[int, int, int, int, str]]:
    """[(left, top, right, bottom, name)] of visible clickable controls named like `name`.
    Exact (case-insensitive) matches first, then names starting with it, then containing it."""
    want = _norm(name)
    if not want:
        return []
    try:
        UIA, auto = _auto()
        ct = UIA.UIA_ControlTypePropertyId
        pc = auto.CreatePropertyCondition
        cond = pc(ct, UIA.UIA_ButtonControlTypeId)
        for t in (UIA.UIA_HyperlinkControlTypeId, UIA.UIA_MenuItemControlTypeId, UIA.UIA_TabItemControlTypeId,
                  UIA.UIA_SplitButtonControlTypeId, UIA.UIA_CheckBoxControlTypeId, UIA.UIA_RadioButtonControlTypeId,
                  UIA.UIA_ListItemControlTypeId):
            cond = auto.CreateOrCondition(cond, pc(ct, t))
        cache = auto.CreateCacheRequest()
        for prop in (UIA.UIA_BoundingRectanglePropertyId, UIA.UIA_IsOffscreenPropertyId, UIA.UIA_NamePropertyId,
                     UIA.UIA_IsEnabledPropertyId):
            cache.AddProperty(prop)
        arr = auto.ElementFromHandle(hwnd).FindAllBuildCache(UIA.TreeScope_Descendants, cond, cache)
        found = []
        for i in range(min(arr.Length, limit)):
            try:
                e = arr.GetElement(i)
                if e.CachedIsOffscreen or not e.GetCachedPropertyValue(UIA.UIA_IsEnabledPropertyId):
                    continue
                label = (e.CachedName or "").strip()
                r = e.CachedBoundingRectangle
            except Exception:
                continue
            got = _norm(label)
            if not got or r.right - r.left < 4 or r.bottom - r.top < 4:
                continue
            rank = 0 if got == want else 1 if got.startswith(want) else 2 if want in got else None
            if rank is not None:
                found.append((rank, len(got), (r.left, r.top, r.right, r.bottom, label[:60])))
        found.sort(key=lambda f: (f[0], f[1]))
        return [f[2] for f in found]
    except Exception as e:  # window gone / UIA unavailable
        log.info("find_clickables failed: %s", e)
        return []


def list_clickables(hwnd: int, limit: int = 60, prefer: str = "", rects: list | None = None) -> list[str]:
    """Labels of the visible buttons / links / tabs in a window (top to bottom), so they can
    plan the next click without a screenshot. Only names, never other text of the window."""
    try:
        UIA, auto = _auto()
        ct = UIA.UIA_ControlTypePropertyId
        pc = auto.CreatePropertyCondition
        cond = pc(ct, UIA.UIA_ButtonControlTypeId)
        for t in (UIA.UIA_HyperlinkControlTypeId, UIA.UIA_TabItemControlTypeId, UIA.UIA_MenuItemControlTypeId):
            cond = auto.CreateOrCondition(cond, pc(ct, t))
        cache = auto.CreateCacheRequest()
        for prop in (UIA.UIA_BoundingRectanglePropertyId, UIA.UIA_IsOffscreenPropertyId, UIA.UIA_NamePropertyId):
            cache.AddProperty(prop)
        arr = auto.ElementFromHandle(hwnd).FindAllBuildCache(UIA.TreeScope_Descendants, cond, cache)
        found, seen = [], set()
        for i in range(min(arr.Length, 600)):
            try:
                e = arr.GetElement(i)
                if e.CachedIsOffscreen:
                    continue
                label = " ".join((e.CachedName or "").split())[:50]
                r = e.CachedBoundingRectangle
            except Exception:
                continue
            if label and label.lower() not in seen and r.right > r.left:
                seen.add(label.lower())
                found.append((r.top, r.left, label, (r.left, r.top, r.right, r.bottom)))
        found.sort()  # top to bottom, left to right
        words = {w for w in _norm(prefer).split() if len(w) > 2}
        if words:  # labels that mention what the user asked for come first
            hit = lambda f: -sum(w in f[2].lower() for w in words)  # noqa: E731
            found.sort(key=hit)
        if rects is not None:  # where each one was (screen px), as a fallback for clicking it later
            rects[:] = [f[3] for f in found[:limit]]
        return [f[2] for f in found[:limit]]
    except Exception as e:
        log.info("list_clickables failed: %s", e)
        return []


def _value_element(x: int, y: int):
    UIA, auto = _auto()
    from ctypes import wintypes
    el = auto.ElementFromPoint(wintypes.POINT(int(x), int(y)))
    walker = auto.ControlViewWalker
    for _ in range(4):  # the point may hit a child of the field (e.g. its text)
        if el is None:
            return None, None
        try:
            if el.GetCurrentPropertyValue(UIA.UIA_IsValuePatternAvailablePropertyId):
                pat = el.GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(UIA.IUIAutomationValuePattern)
                return el, pat
        except Exception:
            pass
        el = walker.GetParentElement(el)
    return None, None


def read_field(x: int, y: int) -> tuple[str | None, str]:
    """(text, problem) of the text field at (x, y). problem: "" | "password" | "unreadable"."""
    try:
        el, pat = _value_element(x, y)
        if pat is None:
            return None, "unreadable"
        if el.CurrentIsPassword:
            return None, "password"
        return str(pat.CurrentValue or ""), ""
    except Exception as e:
        log.info("read_field failed: %s", e)
        return None, "unreadable"


def write_field(x: int, y: int, text: str) -> bool:
    """Replace the field's whole text. False if the field can't be set this way."""
    try:
        el, pat = _value_element(x, y)
        if pat is None or el.CurrentIsPassword or pat.CurrentIsReadOnly:
            return False
        pat.SetValue(text)
        return True
    except Exception as e:
        log.info("write_field failed: %s", e)
        return False


def word_spots(x: int, y: int, words: list[str]) -> dict[str, tuple[int, int, int, int]]:
    """{word: (left, top, right, bottom)} on screen of the first visible occurrence of each word
    in the text field at (x, y), through its Text pattern. Words it can't place are left out
    (fields without a Text pattern give {})."""
    out: dict[str, tuple[int, int, int, int]] = {}
    try:
        UIA, _auto_ = _auto()
        el, _pat = _value_element(x, y)
        if el is None or el.CurrentIsPassword:
            return out
        if not el.GetCurrentPropertyValue(UIA.UIA_IsTextPatternAvailablePropertyId):
            return _edit_spots(el, str(_pat.CurrentValue or ""), words) if _pat is not None else out
        tp = el.GetCurrentPattern(UIA.UIA_TextPatternId).QueryInterface(UIA.IUIAutomationTextPattern)
        doc = tp.DocumentRange
        fr = el.CurrentBoundingRectangle
        for w in words:
            rng = doc.FindText(w, False, False)
            while rng is not None:
                rects = list(rng.GetBoundingRectangles() or ())
                if len(rects) >= 4:  # left, top, width, height (of the first line it covers)
                    l, t, wd, ht = (int(v) for v in rects[:4])
                    if wd > 0 and fr.left - 2 <= l and l + wd <= fr.right + 2 and fr.top - 2 <= t and t + ht <= fr.bottom + 2:
                        out[w] = (l, t, l + wd, t + ht)
                        break
                # not visible (scrolled away): try the next occurrence
                rest = doc.Clone()
                rest.MoveEndpointByRange(UIA.TextPatternRangeEndpoint_Start, rng, UIA.TextPatternRangeEndpoint_End)
                rng = rest.FindText(w, False, False)
    except Exception as e:
        log.info("word_spots failed: %s", e)
    return out


def _edit_spots(el, text: str, words: list[str]) -> dict[str, tuple[int, int, int, int]]:
    """A classic Windows text box (no Text pattern): ask the box itself where a character is
    (EM_POSFROMCHAR). Its coordinates may be scaled for an old, non-DPI-aware app, so they're
    mapped through the ratio of its on-screen size to its client size."""
    import ctypes
    import re
    from ctypes import wintypes
    out: dict[str, tuple[int, int, int, int]] = {}
    hwnd = el.CurrentNativeWindowHandle
    cls = (el.CurrentClassName or "").lower()
    if not hwnd or "edit" not in cls or "rich" in cls:  # RichEdit wants a pointer in another process
        return out
    u32 = ctypes.windll.user32
    send = u32.SendMessageW
    send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    send.restype = ctypes.c_ssize_t
    br = el.CurrentBoundingRectangle
    k = 1.0  # a non-DPI-aware app answers in its own 96-dpi units: scale them to the screen's
    try:
        win_dpi = u32.GetDpiForWindow(wintypes.HWND(hwnd))
        mon_dpi = ctypes.c_uint(96)
        ctypes.windll.shcore.GetDpiForMonitor(u32.MonitorFromWindow(wintypes.HWND(hwnd), 2), 0,
                                              ctypes.byref(mon_dpi), ctypes.byref(ctypes.c_uint()))
        if win_dpi and mon_dpi.value:
            k = mon_dpi.value / win_dpi
    except (OSError, AttributeError):
        pass
    org = wintypes.POINT(0, 0)  # where the client area starts on screen
    u32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(org))
    ox, oy = (org.x, org.y) if br.left - 4 <= org.x <= br.left + 8 else (br.left + 2, br.top + 2)

    def pos(i):
        v = send(hwnd, 0x00D6, i, 0)  # EM_POSFROMCHAR
        if v == -1:
            return None
        x, y = v & 0xFFFF, (v >> 16) & 0xFFFF
        return (x - 0x10000 if x >= 0x8000 else x), (y - 0x10000 if y >= 0x8000 else y)
    for w in words:
        m = re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text)
        if not m:
            continue
        a = pos(m.start())
        if a is None:
            continue
        b = pos(m.end()) or pos(m.end() - 1)
        x2 = b[0] if b and b[1] == a[1] and b[0] > a[0] else a[0] + 9 * len(w)
        line = send(hwnd, 0x00C9, m.start(), 0)        # EM_LINEFROMCHAR
        nxt = send(hwnd, 0x00BB, line + 1, 0)          # EM_LINEINDEX of the next line
        lh = (pos(nxt) or (0, a[1]))[1] - a[1] if nxt > 0 else 0
        if lh <= 0:
            lh = max(14, int(1.9 * (x2 - a[0]) / max(1, len(w))))
        l, t = ox + a[0] * k, oy + a[1] * k
        spot = (int(l), int(t), int(ox + x2 * k), int(t + lh * k))
        if br.top <= spot[1] and spot[3] <= br.bottom + 2 and br.left <= spot[0] < br.right:
            out[w] = spot
    return out


def focused_field() -> dict | None:
    """The text field you're typing in (another app's), read for the spelling watch:
    {text, rect (l, t, r, b), pid}. None for password / read-only fields or non-text controls."""
    try:
        UIA, auto = _auto()
        el = auto.GetFocusedElement()
        if el is None or el.CurrentIsPassword:
            return None
        if el.CurrentControlType not in (UIA.UIA_EditControlTypeId, UIA.UIA_DocumentControlTypeId):
            return None
        if not el.GetCurrentPropertyValue(UIA.UIA_IsValuePatternAvailablePropertyId):
            return None
        pat = el.GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(UIA.IUIAutomationValuePattern)
        if pat.CurrentIsReadOnly:
            return None
        r = el.CurrentBoundingRectangle
        if r.right <= r.left or r.bottom <= r.top:
            return None
        return {"text": str(pat.CurrentValue or "")[:5000], "rect": (r.left, r.top, r.right, r.bottom),
                "pid": int(el.CurrentProcessId)}
    except Exception:
        return None
