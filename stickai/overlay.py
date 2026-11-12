"""Transparent, click-through, always-on-top window - one per monitor.

Why one per monitor? With mixed DPI (say 150% laptop + 100% external),
a single window spanning both is rendered at one scale and gets blurry or
misplaced on the other. One overlay per screen keeps every pixel exact.

Each overlay covers its monitor's *work area* (not the taskbar). A topmost
borderless window that covers a whole monitor can make Windows treat it as
a full-screen app and change taskbar/notification behavior.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QPainter, QScreen
from PySide6.QtWidgets import QWidget

from . import render
from .desktop import win32
from .desktop.win32 import Rect

if TYPE_CHECKING:
    from .app import StickApp

HUD_LOGICAL = QRect(0, 0, 560, 140)


class OverlayWindow(QWidget):
    def __init__(self, screen: QScreen, app: "StickApp"):
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool                       # no taskbar button
            | Qt.WindowType.WindowDoesNotAcceptFocus
            # Click-through is NOT set via Qt's WindowTransparentForInput:
            # we toggle WS_EX_TRANSPARENT ourselves so the overlay can catch
            # clicks while the cursor hovers a stickman.
            | Qt.WindowType.NoDropShadowWindowHint
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.app = app
        self.qscreen = screen
        self.hwnd = 0
        self.phys = Rect(0, 0, 1, 1)  # where this window really is, physical px
        self.scale = 1.0              # physical px per logical px
        self.click_through = False
        self.monitor = None
        self.interactive = False

    def set_interactive(self, on: bool) -> None:
        """Catch mouse input (cursor is over a stickman) or let it fall through."""
        if on == self.interactive or not self.hwnd:
            return
        self.interactive = on
        win32.set_click_through(self.hwnd, not on)
        self.setCursor(Qt.CursorShape.OpenHandCursor if on else Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.app.begin_drag(self)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.app.end_drag()

    def place(self) -> None:
        self.setGeometry(self.qscreen.availableGeometry())
        self.winId()  # create the native window so we can pin it to the screen
        self.windowHandle().setScreen(self.qscreen)
        self.setGeometry(self.qscreen.availableGeometry())
        self.show()
        self.hwnd = int(self.winId())
        self.click_through = win32.make_click_through(self.hwnd)
        self.sync_native_geometry()

    def sync_native_geometry(self) -> None:
        """Read the real physical rect back from Windows instead of trusting
        Qt's logical->physical math; the painter transform is built from it."""
        if not self.hwnd:
            return
        r = win32.window_rect(self.hwnd)
        if r is not None and self.width() > 0:
            self.phys = r
            self.scale = r.width / self.width()
            self.monitor = win32.monitor_for_window(self.hwnd)  # full monitor bounds (for screen grabs)

    def moveEvent(self, e):
        super().moveEvent(e)
        self.sync_native_geometry()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.sync_native_geometry()

    # --------------------------------------------------------- invalidation
    def to_logical(self, r: Rect) -> QRect:
        s = self.scale
        x0 = math.floor((r.left - self.phys.left) / s) - 2
        y0 = math.floor((r.top - self.phys.top) / s) - 2
        x1 = math.ceil((r.right - self.phys.left) / s) + 2
        y1 = math.ceil((r.bottom - self.phys.top) / s) + 2
        return QRect(x0, y0, x1 - x0, y1 - y0)

    def invalidate(self, rects: list[Rect]) -> None:
        """Repaint only where something moved. A layered window re-uploads
        whatever is dirty, so small dirty rects keep CPU usage low."""
        for r in rects:
            if r.intersects(self.phys):
                self.update(self.to_logical(r))

    # ------------------------------------------------------------- painting
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(event.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        p.save()
        p.scale(1.0 / self.scale, 1.0 / self.scale)
        p.translate(-self.phys.left, -self.phys.top)
        world = self.app.world
        if self.app.debug:
            render.paint_debug(p, world.scene, world.stickmen, self.scale)
        if self.app.music.running:
            self.app.music.paint(p)
        for s in world.stickmen:
            if s.bbox().intersects(self.phys):
                render.paint_stickman(p, s)
                if self.app.debug:
                    render.paint_stickman_debug(p, s)
        self.app.fight.paint(p)  # hit sparks and kick swooshes over the fighters
        self.app.couch.paint(p)  # in front of them: only heads peek over the back
        p.restore()

        if self.app.debug and self.app.hud_overlay is self:
            p.scale(1.0 / self.scale, 1.0 / self.scale)
            render.paint_hud(p, 12 * self.scale, 12 * self.scale, self.scale, self.app.hud_lines())
        p.end()
