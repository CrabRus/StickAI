"""Notifications in a corner of the screen while the chat window is closed.

Small dark cards (the speaker's colour on the left, name, the line), stacked in the
chosen corner, fading out after a while. They never take the keyboard focus; a click
opens the chat. Optional sound (the Windows notification sound).
"""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import QWidget

from . import theme

WIDTH = 330
MARGIN = 16
GAP = 8
MAX_SHOWN = 3
MAX_CHARS = 240
CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")


def play_sound() -> None:
    try:
        import winsound
        winsound.PlaySound("Notification.Default", winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:  # noqa: BLE001 - no sound device / scheme: try the plain beep
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:  # noqa: BLE001
            pass


class Toast(QWidget):
    def __init__(self, manager: "Toasts", name: str, color: str, text: str):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manager = manager
        self.name, self.color = name, QColor(color)
        self.text = text if len(text) <= MAX_CHARS else text[:MAX_CHARS - 1] + "…"
        self.f_name = QFont("Segoe UI", 10)
        self.f_name.setWeight(QFont.Weight.DemiBold)
        self.f_text = QFont("Segoe UI", 10)
        inner = WIDTH - 34
        th = QFontMetrics(self.f_text).boundingRect(0, 0, inner, 2000, Qt.TextFlag.TextWordWrap, self.text).height()
        self.setFixedSize(WIDTH, min(220, 34 + th + 12))
        self.setWindowOpacity(0.0)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(220)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._closing = False
        seconds = min(12.0, 4.0 + len(self.text) / 22.0)
        QTimer.singleShot(int(seconds * 1000), self.dismiss)

    def appear(self) -> None:
        self.show()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(0.96)
        self._fade.start()

    def dismiss(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._gone)
        self._fade.start()

    def _gone(self) -> None:
        self.manager._remove(self)
        self.close()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.manager.clicked()
        else:
            self.dismiss()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QColor(theme.BORDER))
        p.setBrush(QColor(theme.SURFACE))
        p.drawRoundedRect(r, 12, 12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.color)
        p.drawRoundedRect(QRectF(8, 10, 5, self.height() - 20), 2.5, 2.5)
        p.setPen(self.color)
        p.setFont(self.f_name)
        p.drawText(QRectF(22, 8, WIDTH - 34, 22), Qt.AlignmentFlag.AlignVCenter, self.name)
        p.setPen(QColor(theme.TEXT))
        p.setFont(self.f_text)
        p.drawText(QRectF(22, 32, WIDTH - 34, self.height() - 40), Qt.TextFlag.TextWordWrap, self.text)


class Toasts:
    def __init__(self, on_click):
        self.on_click = on_click
        self.shown: list[Toast] = []
        self.corner = "bottom-right"

    def show(self, name: str, color: str, text: str, sound: bool = False) -> None:
        t = Toast(self, name, color, text)
        self.shown.append(t)
        while len([x for x in self.shown if not x._closing]) > MAX_SHOWN:
            next(x for x in self.shown if not x._closing).dismiss()
        self._layout()
        t.appear()
        if sound:
            play_sound()

    def clicked(self) -> None:
        self.clear()
        self.on_click()

    def clear(self) -> None:
        for t in list(self.shown):
            t.dismiss()

    def _remove(self, t: Toast) -> None:
        if t in self.shown:
            self.shown.remove(t)
            self._layout()

    def _layout(self) -> None:
        geo = QGuiApplication.primaryScreen().availableGeometry()
        right = "right" in self.corner
        bottom = "bottom" in self.corner
        x = geo.right() - WIDTH - MARGIN if right else geo.left() + MARGIN
        y = geo.bottom() - MARGIN if bottom else geo.top() + MARGIN
        for t in reversed(self.shown):  # the newest sits in the corner
            if bottom:
                y -= t.height()
                t.move(x, y)
                y -= GAP
            else:
                t.move(x, y)
                y += t.height() + GAP
