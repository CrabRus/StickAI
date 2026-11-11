"""Finds surfaces *inside* windows with Windows UI Automation, on a
background thread.

Most modern apps (Chrome, Edge, Explorer, WinUI/WPF/Qt apps) draw their own
controls instead of using child HWNDs, but they all describe them through
UI Automation, the API screen readers use. For web browsers this is the
page's accessibility tree, i.e. the HTML structure: headings (<h1>-<h3>),
images (<img>), buttons and links.

Kinds reported:
  field    single-line text fields / search bars / drop-downs (all apps)
  heading  page headings              (browsers / Electron apps)
  image    pictures                   (browsers)
  button   buttons                    (browsers)

Rectangles are *relative to their window* so they stay correct while the
window moves between scans. We read geometry, the control kind and its
accessible name (e.g. "Search"), never typed text or page content.
"""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal

log = logging.getLogger("stickai.widgets")

SCAN_EVERY = 2.0          # seconds between scan rounds
MAX_WINDOWS = 5           # topmost visible windows scanned per round
BROWSER_CLASSES = {"Chrome_WidgetWin_1", "MozillaWindowClass"}  # Chrome, Edge, Brave, Opera, Electron, Firefox
LIMITS = {"field": 8, "heading": 10, "image": 8, "button": 8}
# (min width, min height, max height) in physical px per kind
SIZES = {"field": (80, 14, 140), "heading": (140, 18, 200), "image": (120, 40, 2000), "button": (90, 22, 120)}


class WidgetScanner(QObject):
    """Feed it windows with set_targets(); it emits
    found = {hwnd: [(dx, dy, w, h, kind, name), ...]} (rects relative to the window)."""

    found = Signal(object)

    def __init__(self):
        super().__init__()
        self._targets: list[tuple[int, tuple[int, int, int, int], str]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._enabled = threading.Event()
        self._web = True
        self._thread = threading.Thread(target=self._run, name="uia-widgets", daemon=True)
        self.available = True

    def start(self) -> None:
        self._thread.start()

    def set_enabled(self, on: bool, web: bool = True) -> None:
        self._web = web
        (self._enabled.set if on else self._enabled.clear)()
        if not on:
            self.found.emit({})

    def set_targets(self, windows) -> None:
        """windows: [(hwnd, (left, top, right, bottom), class_name)], topmost first."""
        with self._lock:
            self._targets = list(windows)[:MAX_WINDOWS]

    def stop(self) -> None:
        self._stop.set()
        self._enabled.set()  # wake the loop so it can exit

    # ------------------------------------------------------------ worker
    def _run(self) -> None:
        try:
            import comtypes
            import comtypes.client
            try:
                comtypes.CoInitialize()  # error if `import comtypes` already did it for this thread
            except OSError:
                pass
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as UIA
            auto = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        except Exception as e:  # comtypes missing / UIA unavailable
            log.warning("UI Automation unavailable, widgets disabled: %s", e)
            self.available = False
            return

        ctype = UIA.UIA_ControlTypePropertyId
        heading = UIA.UIA_HeadingLevelPropertyId
        pc = auto.CreatePropertyCondition

        def any_of(*conds):
            c = conds[0]
            for d in conds[1:]:
                c = auto.CreateOrCondition(c, d)
            return c

        fields = any_of(pc(ctype, UIA.UIA_EditControlTypeId), pc(ctype, UIA.UIA_ComboBoxControlTypeId))
        web = any_of(fields, pc(ctype, UIA.UIA_ImageControlTypeId), pc(ctype, UIA.UIA_ButtonControlTypeId),
                     pc(heading, UIA.HeadingLevel1), pc(heading, UIA.HeadingLevel1 + 1),
                     pc(heading, UIA.HeadingLevel1 + 2))
        cache = auto.CreateCacheRequest()
        for prop in (UIA.UIA_BoundingRectanglePropertyId, UIA.UIA_IsOffscreenPropertyId, ctype, heading,
                     UIA.UIA_NamePropertyId):
            cache.AddProperty(prop)
        kind_of = {UIA.UIA_EditControlTypeId: "field", UIA.UIA_ComboBoxControlTypeId: "field",
                   UIA.UIA_ImageControlTypeId: "image", UIA.UIA_ButtonControlTypeId: "button"}

        while not self._stop.is_set():
            self._enabled.wait()
            if self._stop.is_set():
                break
            with self._lock:
                targets = list(self._targets)
            result: dict[int, list[tuple]] = {}
            for hwnd, (wl, wt, wr, wb), cls in targets:
                is_web = self._web and cls in BROWSER_CLASSES
                try:
                    el = auto.ElementFromHandle(hwnd)
                    arr = el.FindAllBuildCache(UIA.TreeScope_Descendants, web if is_web else fields, cache)
                except Exception:
                    continue  # window closed / app busy
                by_kind: dict[str, list[tuple]] = {}
                for i in range(min(arr.Length, 400)):
                    try:
                        e = arr.GetElement(i)
                        if e.CachedIsOffscreen:
                            continue
                        r = e.CachedBoundingRectangle
                        lvl = e.GetCachedPropertyValue(heading)
                        kind = "heading" if lvl and UIA.HeadingLevel1 <= lvl <= UIA.HeadingLevel1 + 2 \
                            else kind_of.get(e.CachedControlType)
                        name = (e.CachedName or "")[:40]
                    except Exception:
                        continue
                    if kind is None:
                        continue
                    left, right = max(r.left, wl), min(r.right, wr)
                    w, h = right - left, r.bottom - r.top
                    min_w, min_h, max_h = SIZES[kind]
                    # sized right, inside the window horizontally, top edge visible below its top
                    if w < min_w or not min_h <= h <= max_h or r.top < wt + 8 or r.top > wb - 10:
                        continue
                    by_kind.setdefault(kind, []).append((left - wl, r.top - wt, w, min(h, wb - r.top), kind, name))
                kept: list[tuple] = []
                for kind, items in by_kind.items():
                    items.sort(key=lambda q: -q[2])  # widest first
                    n = 0
                    for q in items:
                        # drop near-duplicates (a combo box and its edit, nested images...)
                        if any(abs(q[1] - k[1]) <= 6 and q[0] < k[0] + k[2] and k[0] < q[0] + q[2] for k in kept):
                            continue
                        kept.append(q)
                        n += 1
                        if n >= LIMITS[kind]:
                            break
                if kept:
                    result[hwnd] = kept
            self.found.emit(result)
            self._stop.wait(SCAN_EVERY)


# ---------------------------------------------------------------------------
# One-off query used when a game starts: where is the actual page / document
# inside a window (below a browser's tabs, address bar and extension icons)?
_local = threading.local()


def content_rect(hwnd: int):
    """Largest UI Automation Document element in the window, as
    (left, top, right, bottom) physical px, or None. Runs on the calling thread."""
    try:
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
        UIA, auto = _local.UIA, _local.auto
        el = auto.ElementFromHandle(hwnd)
        cond = auto.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_DocumentControlTypeId)
        arr = el.FindAll(UIA.TreeScope_Descendants, cond)
        best = None
        for i in range(min(arr.Length, 20)):
            r = arr.GetElement(i).CurrentBoundingRectangle
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > 0 and (best is None or area > best[0]):
                best = (area, (r.left, r.top, r.right, r.bottom))
        return best[1] if best else None
    except Exception as e:  # UIA unavailable / window gone
        log.debug("content_rect failed: %s", e)
        return None
