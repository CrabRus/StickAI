"""Right-click a stickman: a small box to say something to just him.

A popup next to the cursor with his name in his colour and a one-line input.
Enter sends (the message also shows in the chat history), Esc or a click elsewhere closes it.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from ..i18n import tr
from . import theme


class QuickAsk(QWidget):
    sent = Signal(str, str)  # character key, text

    def __init__(self, key: str, name: str, color: str):
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.key = key
        self.setObjectName("QuickAsk")
        self.setStyleSheet(f"""
QWidget#QuickAsk {{ background: {theme.SURFACE}; border: 1px solid {color}; border-radius: 10px; }}
QLabel {{ color: {color}; font: 600 10pt 'Segoe UI'; background: transparent; }}
QLineEdit {{ background: {theme.FIELD}; color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 8px;
             padding: 6px 8px; font: 10.5pt 'Segoe UI'; selection-background-color: {theme.ACCENT}; }}
QLineEdit:focus {{ border: 1px solid {color}; }}
QPushButton {{ background: {color}; color: #1b1b1b; border: none; border-radius: 8px; padding: 6px 12px;
               font: bold 9.5pt 'Segoe UI'; }}
""")
        title = QLabel(tr("ask.to", name=name))
        self.edit = QLineEdit()
        self.edit.setMinimumWidth(300)
        self.edit.returnPressed.connect(self._send)
        go = QPushButton("➤")
        go.setCursor(Qt.CursorShape.PointingHandCursor)
        go.clicked.connect(self._send)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.edit, 1)
        row.addWidget(go)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)
        lay.addWidget(title)
        lay.addLayout(row)

    def open_at_cursor(self) -> None:
        self.adjustSize()
        pos = QCursor.pos()
        geo = QGuiApplication.screenAt(pos).availableGeometry() if QGuiApplication.screenAt(pos) else \
            QGuiApplication.primaryScreen().availableGeometry()
        x = min(max(pos.x() + 12, geo.left() + 8), geo.right() - self.width() - 8)
        y = min(max(pos.y() - self.height() - 12, geo.top() + 8), geo.bottom() - self.height() - 8)
        self.move(x, y)
        self.show()
        self.activateWindow()
        self.edit.setFocus()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)

    def _send(self) -> None:
        text = self.edit.text().strip()
        if text:
            self.sent.emit(self.key, text)
        self.close()
