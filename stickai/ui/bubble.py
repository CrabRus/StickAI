"""Speech bubble: a small top-level window in the speaker's colour that
follows his head around (positioned in physical pixels via Win32)."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ..desktop import win32

MAX_TEXT_W = 300
PAD_X, PAD_Y = 15, 11
TAIL = 12
RADIUS = 14
SHADOW = 4                 # px of soft shadow under the bubble
PAPER = QColor(252, 252, 250)   # light "comic" bubble: dark text on it reads on any wallpaper
INK = QColor(28, 28, 34)
STILL_PX = 48              # logical px: the bubble's dead zone is a third of this (idle swaying stays inside)
FOLLOW = 0.35              # per frame: how quickly a moving bubble catches up with the head


def _text_color(bg: QColor) -> QColor:
    # relative luminance -> dark text on light colours, white on dark ones
    lum = 0.2126 * bg.redF() ** 2.2 + 0.7152 * bg.greenF() ** 2.2 + 0.0722 * bg.blueF() ** 2.2
    return QColor(20, 20, 24) if lum > 0.22 else QColor(255, 255, 255)


TIME_FACTOR = 1.0  # Settings -> how long bubbles stay (x0.5 .. x3)


def reading_time(text: str) -> float:
    return max(3.5, min(14.0, 2.0 + 0.065 * len(text))) * TIME_FACTOR


class SpeechBubble(QWidget):
    def __init__(self, color: str):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("click to dismiss")
        self.bg = QColor(color)   # the character's colour: border + the "!" / thinking dots
        self.fg = _text_color(self.bg)
        self.font_ = QFont("Segoe UI", 11)
        self.font_.setWeight(QFont.Weight.Medium)
        self.font_.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        self._smooth = None  # [x, y] the bubble follows (smoothed head position)
        self._chasing = False
        self.on_click = None  # e.g. "show me that line in VS Code"; else a click dismisses
        self._head = None     # the last (head_x, head_top, work) it was placed for
        self.text = ""
        self.thinking = False
        self.indicator = False
        self.big_font = QFont("Segoe UI", 15)
        self.big_font.setWeight(QFont.Weight.Black)
        self.expires = 0.0
        self.tail_x = 0.0  # logical px from the left edge
        self._last_pos = None

    # ------------------------------------------------------------ content
    def set_color(self, color: str) -> None:
        self.bg = QColor(color)
        self.fg = _text_color(self.bg)
        self.update()

    def say(self, text: str) -> None:
        self.thinking = False
        self.indicator = False
        self.text = text
        self.expires = time.monotonic() + reading_time(text)
        self._relayout()

    def indicate(self, mark: str) -> None:
        """Small bubble with just "!" or "?" (the text is in the chat window)."""
        self.thinking = False
        self.indicator = True
        self.text = mark
        self.expires = time.monotonic() + 4.5
        self._relayout()

    def think(self) -> None:
        self.thinking = True
        self.text = ""
        self.expires = time.monotonic() + 60
        self._relayout()

    @property
    def expired(self) -> bool:
        return time.monotonic() > self.expires

    def _text_rect(self) -> QRectF:
        if self.thinking:
            return QRectF(0, 0, 44, QFontMetrics(self.font_).height())
        if self.indicator:
            fm = QFontMetrics(self.big_font)
            return QRectF(0, 0, max(fm.horizontalAdvance(self.text), 8), fm.height() - 8)
        fm = QFontMetrics(self.font_)
        r = fm.boundingRect(0, 0, MAX_TEXT_W, 10_000, Qt.TextFlag.TextWordWrap, self.text)
        return QRectF(0, 0, r.width() + 4, r.height() + 2)

    def _relayout(self) -> None:
        tr = self._text_rect()
        # + border (1.5 px each side) and shadow, so the text gets exactly the width it was measured for
        self.setFixedSize(int(tr.width() + 2 * PAD_X + SHADOW + 4), int(tr.height() + 2 * PAD_Y + TAIL + SHADOW + 4))
        self._last_pos = None
        self._smooth = None  # new text: placed exactly, then followed smoothly
        self.winId()  # the native window exists (hidden) so it can be placed before it's shown
        if self._head is not None:
            self.follow(*self._head)  # resized: re-place right now, not one frame later (no jump)
        self.update()

    def mousePressEvent(self, _e) -> None:
        if self.on_click is not None:
            action, self.on_click = self.on_click, None
            action()
        self.expires = 0.0  # click to dismiss

    # ----------------------------------------------------------- placement
    def follow(self, head_x: float, head_top: float, work: win32.Rect) -> None:
        """Put the tail tip just above the head (physical px); shown only once it's in place."""
        self._head = (head_x, head_top, work)
        hwnd = int(self.winId())
        r = win32.window_rect(hwnd)
        if r is None or self.width() <= 0:
            return
        if self.isVisible():
            scale = r.width / self.width()
        else:  # not shown yet: the native size may not be scaled for the monitor yet
            scale = self.devicePixelRatioF() or 1.0
            r = win32.Rect(r.left, r.top, r.left + int(self.width() * scale), r.top + int(self.height() * scale))
        w, h = r.width, r.height
        # Smooth follow: at rest it stays put (breathing / swaying stays inside the dead zone); once the
        # head really moves it glides after it every frame - never in steps.
        tx, ty = head_x, head_top
        if self._smooth is None:
            self._smooth = [tx, ty]
            self._chasing = False
        sx, sy = self._smooth
        dead = STILL_PX * 0.3 * scale
        if abs(tx - sx) > dead or abs(ty - sy) > dead:
            self._chasing = True
        if self._chasing:
            sx += (tx - sx) * FOLLOW
            sy += (ty - sy) * FOLLOW
            if abs(tx - sx) < 1.0 and abs(ty - sy) < 1.0:
                sx, sy, self._chasing = tx, ty, False
            self._smooth = [sx, sy]
        head_x, head_top = sx, sy
        x = int(min(max(head_x - w / 2, work.left + 4), work.right - w - 4))
        y = int(max(head_top - h - 4 * scale, work.top + 4))
        tail = (tx - x) / scale  # the tail points at the real head, even while the bubble catches up
        tail = min(max(tail, RADIUS + 8), self.width() - RADIUS - 8)
        if abs(tail - self.tail_x) > 0.5:
            self.tail_x = tail
            self.update()
        if (x, y) != self._last_pos:
            self._last_pos = (x, y)
            win32.user32.SetWindowPos(hwnd, win32.HWND_TOPMOST, x, y, 0, 0,
                                      win32.SWP_NOSIZE | win32.SWP_NOACTIVATE)
        if not self.isVisible():  # first placement done: now show it, already in the right spot
            self.show()
            win32.set_no_activate(hwnd)  # clicking it never steals focus
        if self.thinking:
            self.update()  # animate the dots

    # ------------------------------------------------------------ painting
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        body = QRectF(1.5, 1.5, self.width() - 3 - SHADOW, self.height() - TAIL - 1.5 - SHADOW)
        path = QPainterPath()
        path.addRoundedRect(body, RADIUS, RADIUS)
        tail = QPainterPath()
        tx = self.tail_x or (self.width() - SHADOW) / 2
        tail.moveTo(tx - 9, body.bottom() - 1)
        tail.quadTo(tx - 2, body.bottom() + TAIL * 0.4, tx, body.bottom() + TAIL - 1)
        tail.quadTo(tx + 2, body.bottom() + TAIL * 0.4, tx + 9, body.bottom() - 1)
        tail.closeSubpath()
        path = path.united(tail)
        for i, a in ((3, 18), (2, 26), (1, 34)):  # soft shadow: separates it from any background
            p.fillPath(path.translated(i, i + 0.5), QColor(0, 0, 0, a))
        p.fillPath(path, PAPER)
        p.setPen(QPen(self.bg, 2.2))
        p.drawPath(path)

        if self.thinking:
            t = time.monotonic()
            cy = body.center().y()
            for i in range(3):
                a = 0.35 + 0.65 * max(0.0, math.sin(t * 5 - i * 0.7))
                c = QColor(self.bg.darker(110))
                c.setAlphaF(a)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawEllipse(QPointF(body.center().x() + (i - 1) * 12, cy), 3.6, 3.6)
            return
        if self.indicator:
            p.setPen(self.bg.darker(115))
            p.setFont(self.big_font)
            p.drawText(body, Qt.AlignmentFlag.AlignCenter, self.text)
            return
        p.setPen(INK)
        p.setFont(self.font_)
        p.drawText(body.adjusted(PAD_X, PAD_Y, -PAD_X, -PAD_Y), Qt.TextFlag.TextWordWrap, self.text)
