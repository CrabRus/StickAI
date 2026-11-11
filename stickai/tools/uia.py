"""One-off Windows UI Automation queries used by tasks (run them on a worker thread).

* find_clickables(hwnd, name): buttons / links / tabs / menu items whose
  accessible name matches, best match first ("press Save", "click Next").
* read_field(x, y) / write_field(x, y, text): the text field at a screen point,
  through its Value pattern. Used only by "check the spelling in that field",
  which is a separate opt-in setting. Password fields are never read.
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
