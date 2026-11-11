"""Speech bubble: a small top-level window in the speaker's colour that
follows his head around (positioned in physical pixels via Win32)."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ..desktop import win32

MAX_TEXT_W = 280
PAD_X, PAD_Y = 14, 10
TAIL = 12
RADIUS = 14


def _text_color(bg: QColor) -> QColor:
    # relative luminance -> dark text on light colours, white on dark ones
    lum = 0.2126 * bg.redF() ** 2.2 + 0.7152 * bg.greenF() ** 2.2 + 0.0722 * bg.blueF() ** 2.2
    return QColor(20, 20, 24) if lum > 0.22 else QColor(255, 255, 255)


def reading_time(text: str) -> float:
    return max(3.5, min(14.0, 2.0 + 0.065 * len(text)))


class SpeechBubble(QWidget):
    def __init__(self, color: str):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("click to dismiss")
        self.bg = QColor(color)
        self.fg = _text_color(self.bg)
        self.font_ = QFont("Segoe UI", 11)
        self.font_.setWeight(QFont.Weight.DemiBold)
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
        return QRectF(0, 0, r.width() + 2, r.height())

    def _relayout(self) -> None:
        tr = self._text_rect()
        self.setFixedSize(int(tr.width() + 2 * PAD_X), int(tr.height() + 2 * PAD_Y + TAIL))
        self._last_pos = None
        if not self.isVisible():
            self.show()
            win32.set_no_activate(int(self.winId()))  # clicking it never steals focus
        self.update()

    def mousePressEvent(self, _e) -> None:
        self.expires = 0.0  # click to dismiss

    # ----------------------------------------------------------- placement
    def follow(self, head_x: float, head_top: float, work: win32.Rect) -> None:
        """Put the tail tip just above the head (physical px)."""
        hwnd = int(self.winId())
        r = win32.window_rect(hwnd)
        if r is None or self.width() <= 0:
            return
        scale = r.width / self.width()
        w, h = r.width, r.height
        x = int(min(max(head_x - w / 2, work.left + 4), work.right - w - 4))
        y = int(max(head_top - h - 4 * scale, work.top + 4))
        tail = (head_x - x) / scale
        tail = min(max(tail, RADIUS + 8), self.width() - RADIUS - 8)
        if abs(tail - self.tail_x) > 0.5:
            self.tail_x = tail
            self.update()
        if (x, y) != self._last_pos:
            self._last_pos = (x, y)
            win32.user32.SetWindowPos(hwnd, win32.HWND_TOPMOST, x, y, 0, 0,
                                      win32.SWP_NOSIZE | win32.SWP_NOACTIVATE)
        if self.thinking:
            self.update()  # animate the dots

    # ------------------------------------------------------------ painting
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(1, 1, self.width() - 2, self.height() - TAIL - 1)
        path = QPainterPath()
        path.addRoundedRect(body, RADIUS, RADIUS)
        tail = QPainterPath()
        tx = self.tail_x or self.width() / 2
        tail.moveTo(tx - 9, body.bottom() - 1)
        tail.quadTo(tx - 2, body.bottom() + TAIL * 0.4, tx, body.bottom() + TAIL - 1)
        tail.quadTo(tx + 2, body.bottom() + TAIL * 0.4, tx + 9, body.bottom() - 1)
        tail.closeSubpath()
        path = path.united(tail)
        p.fillPath(path, self.bg)
        p.setPen(QPen(self.bg.darker(135), 1.5))
        p.drawPath(path)

        if self.thinking:
            t = time.monotonic()
            cy = body.center().y()
            for i in range(3):
                a = 0.35 + 0.65 * max(0.0, math.sin(t * 5 - i * 0.7))
                c = QColor(self.fg)
                c.setAlphaF(a)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawEllipse(QPointF(body.center().x() + (i - 1) * 12, cy), 3.6, 3.6)
            return
        p.setPen(self.fg)
        if self.indicator:
            p.setFont(self.big_font)
            p.drawText(body, Qt.AlignmentFlag.AlignCenter, self.text)
            return
        p.setFont(self.font_)
        p.drawText(body.adjusted(PAD_X, PAD_Y, -PAD_X, -PAD_Y), Qt.TextFlag.TextWordWrap, self.text)
