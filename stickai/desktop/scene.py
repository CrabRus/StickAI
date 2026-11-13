"""Turns the raw list of OS windows into walkable geometry.

A *platform* is a horizontal segment a stickman can stand on:
  * the top edge of every visible window, minus the parts hidden behind
    windows that are higher in the Z-order;
  * the bottom of each monitor's work area (i.e. on top of the taskbar).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import win32
from .win32 import MonitorInfo, Rect, WindowInfo

FLOOR = 0  # hwnd used for monitor floors
MIN_PLATFORM_WIDTH = 24


@dataclass(frozen=True, slots=True)
class Platform:
    y: int
    x0: int
    x1: int
    hwnd: int  # 0 == monitor floor
    widget: bool = False  # top of something inside the window (search bar, web heading...)
    kind: str = ""        # widget kind: field | heading | image | button

    @property
    def is_floor(self) -> bool:
        return self.hwnd == FLOOR

    def shifted(self, dx: int, dy: int) -> "Platform":
        return Platform(self.y + dy, self.x0 + dx, self.x1 + dx, self.hwnd, self.widget, self.kind)


@dataclass(frozen=True, slots=True)
class Wall:
    """A visible vertical window edge.

    side = +1 for a window's LEFT edge (met by someone walking right),
    side = -1 for its RIGHT edge. `climbable` means the edge reaches the
    window's top corner and the top edge there is walkable, so a climber can
    pull himself up at the end.
    """
    x: int
    y0: int
    y1: int
    hwnd: int
    side: int
    climbable: bool

    def shifted(self, dx: int, dy: int) -> "Wall":
        return Wall(self.x + dx, self.y0 + dy, self.y1 + dy, self.hwnd, self.side, self.climbable)


def _subtract(spans: list[tuple[int, int]], a: int, b: int) -> list[tuple[int, int]]:
    out = []
    for s0, s1 in spans:
        if b <= s0 or a >= s1:
            out.append((s0, s1))
            continue
        if a > s0:
            out.append((s0, a))
        if b < s1:
            out.append((b, s1))
    return out


def build_geometry(windows: list[WindowInfo], monitors: list[MonitorInfo], headroom: int,
                   widgets: dict[int, list[tuple[int, int, int, int]]] | None = None
                   ) -> tuple[list[Platform], list[Wall]]:
    platforms: list[Platform] = []
    walls: list[Wall] = []
    above: list[Rect] = []  # windows earlier in Z-order (closer to the viewer)

    for w in windows:
        r = w.rect
        # --- top edge -> platforms, minus parts covered by windows above it
        y = r.top
        spans = [(r.left, r.right)]
        for c in above:
            if c.top <= y < c.bottom:
                spans = _subtract(spans, c.left, c.right)
                if not spans:
                    break
        mine: list[Platform] = []
        for m in monitors:
            wa = m.work
            if not (wa.top + headroom <= y < wa.bottom - 4):
                continue
            for s0, s1 in spans:
                a, b = max(s0, wa.left), min(s1, wa.right)
                if b - a >= MIN_PLATFORM_WIDTH:
                    mine.append(Platform(y, a, b, w.hwnd))
        platforms.extend(mine)

        # --- widgets inside the window (search bars...): their top edges,
        #     positioned relative to the window, hidden where covered
        for wd in (widgets or {}).get(w.hwnd, ()):
            dx, dy, ww = wd[0], wd[1], wd[2]
            kind = wd[4] if len(wd) > 4 else "field"
            wy = r.top + dy
            wspans = [(r.left + dx, r.left + dx + ww)]
            for c in above:
                if c.top <= wy < c.bottom:
                    wspans = _subtract(wspans, c.left, c.right)
            for m in monitors:
                wa = m.work
                if not (wa.top + 4 <= wy < wa.bottom - 4):
                    continue
                for s0, s1 in wspans:
                    a, b = max(s0, wa.left, r.left), min(s1, wa.right, r.right)
                    if b - a >= MIN_PLATFORM_WIDTH:
                        platforms.append(Platform(wy, a, b, w.hwnd, widget=True, kind=kind))

        # --- side edges -> walls, minus parts covered by windows above
        for side, x in ((1, r.left), (-1, r.right)):
            vspans = [(r.top, r.bottom)]
            for c in above:
                if c.left <= x < c.right:
                    vspans = _subtract(vspans, c.top, c.bottom)
            if side == 1:
                top_ok = any(p.x0 <= x + 2 and p.x1 - x >= 30 for p in mine)
            else:
                top_ok = any(p.x1 >= x - 2 and x - p.x0 >= 30 for p in mine)
            for m in monitors:
                wa = m.work
                if not (wa.left <= x <= wa.right):
                    continue
                for s0, s1 in vspans:
                    a, b = max(s0, wa.top + headroom), min(s1, wa.bottom)
                    if b - a >= MIN_PLATFORM_WIDTH:
                        walls.append(Wall(x, a, b, w.hwnd, side, top_ok and a == r.top))
        above.append(r)

    for m in monitors:
        platforms.append(Platform(m.work.bottom, m.work.left, m.work.right, FLOOR))
    return platforms, walls


class DesktopScene:
    """Snapshot of the desktop, refreshed by polling (~20 Hz)."""

    def __init__(self, exclude_pid: int, headroom: int = 0):
        self.exclude_pid = exclude_pid
        self.headroom = headroom
        self.monitors: list[MonitorInfo] = []
        self.windows: list[WindowInfo] = []
        self.platforms: list[Platform] = []
        self.walls: list[Wall] = []
        self.foreground: WindowInfo | None = None
        self._by_hwnd: dict[int, WindowInfo] = {}
        self._signature = None
        self.widgets: dict[int, list[tuple[int, int, int, int]]] = {}  # hwnd -> rects relative to window

    def refresh(self) -> bool:
        """Re-poll the OS. Returns True if anything visible changed."""
        monitors = win32.enumerate_monitors()
        windows = win32.enumerate_windows(self.exclude_pid)
        fg = win32.foreground_window()
        sig = (tuple(monitors), tuple((w.hwnd, w.rect) for w in windows), fg)
        if sig == self._signature:
            return False
        self._signature = sig
        self.monitors, self.windows = monitors, windows
        self._by_hwnd = {w.hwnd: w for w in windows}
        self.foreground = self._by_hwnd.get(fg)
        self._rebuild()
        return True

    def set_widgets(self, widgets: dict[int, list[tuple[int, int, int, int]]]) -> bool:
        """New scan results from the widget scanner. Returns True if changed."""
        if widgets == self.widgets:
            return False
        self.widgets = widgets
        self._rebuild()
        return True

    def _rebuild(self) -> None:
        self.platforms, self.walls = build_geometry(self.windows, self.monitors, self.headroom, self.widgets)
        self.platforms += getattr(self, "extra", [])

    def set_extra(self, platforms: list, owner: str = "couch") -> None:
        """App-made surfaces that aren't windows (the couch seat, the card table...; negative
        hwnd). Each owner keeps its own list, so one activity doesn't wipe another's."""
        lists = getattr(self, "_extras", {})
        lists[owner] = list(platforms)
        self._extras = lists
        self.extra = [p for ps in lists.values() for p in ps]
        self._rebuild()

    def window(self, hwnd: int) -> WindowInfo | None:
        return self._by_hwnd.get(hwnd)

    def platform_under(self, x: float, y: float, hwnd: int, tolerance: float = 6.0,
                       window_tolerance: float = 1e9) -> Platform | None:
        """The platform of `hwnd` under x nearest to y. A window can have several
        (its top edge plus widgets), so the closest one in height wins."""
        best, best_d = None, None
        for p in self.platforms:
            if p.hwnd != hwnd or not p.x0 <= x <= p.x1:
                continue
            d = abs(p.y - y)
            if d <= (tolerance if hwnd == FLOOR else window_tolerance) and (best_d is None or d < best_d):
                best, best_d = p, d
        return best

    def widget_targets(self) -> list[Platform]:
        return [p for p in self.platforms if p.widget]

    def landing(self, x: float, y_from: float, y_to: float) -> Platform | None:
        """Highest platform crossed while falling from y_from to y_to at x."""
        best = None
        for p in self.platforms:
            if p.x0 <= x <= p.x1 and y_from - 0.5 <= p.y <= y_to and (best is None or p.y < best.y):
                best = p
        return best

    def floor_under(self, x: float, y: float) -> Platform | None:
        """The monitor floor below (x, y), used as a safety net when a thrown
        stickman starts below a floor (e.g. released over the taskbar)."""
        floors = sorted((p for p in self.platforms if p.is_floor and p.x0 <= x <= p.x1), key=lambda p: p.y)
        for p in floors:
            if p.y >= y - 400:
                return p
        return floors[-1] if floors else None

    def in_work_area(self, x: float, y: float) -> bool:
        return any(m.work.contains(x, y) for m in self.monitors)

    @property
    def highest_top(self) -> int:
        return min((m.work.top for m in self.monitors), default=0)

    @property
    def lowest_floor(self) -> int:
        return max((m.work.bottom for m in self.monitors), default=0)
