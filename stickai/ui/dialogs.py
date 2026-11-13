"""Styled confirm / permission dialogs that match the chat and settings windows.

Replaces plain QMessageBox, which ignored the dark theme (and, parented to the
chat window, inherited its light label colour on a light box: unreadable).
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import theme

_ACCEPTED = QDialog.DialogCode.Accepted.value  # exec()/finished give a plain int

STYLE = f"""
QDialog#StickDialog {{ background: {theme.BG}; }}
QLabel#DlgTitle {{ color: #ffffff; font: 600 12pt 'Segoe UI'; }}
QLabel#DlgText {{ color: #d6d8dd; font: 10.5pt 'Segoe UI'; }}
QPushButton {{ background: #2f343e; color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 8px;
               padding: 7px 18px; font: 10pt 'Segoe UI'; min-width: 72px; }}
QPushButton:hover {{ background: {theme.BORDER}; }}
QPushButton:focus {{ border: 1px solid #6b7180; }}
QPushButton#Primary {{ background: {theme.ACCENT}; color: #1b1b1b; border: none; font: bold 10pt 'Segoe UI'; }}
QPushButton#Primary:hover {{ background: {theme.ACCENT_HOVER}; }}
QPushButton#Danger {{ background: {theme.DANGER_BG}; color: #ffd6d2; border: 1px solid #7a3a3a; font: bold 10pt 'Segoe UI'; }}
QPushButton#Danger:hover {{ background: #6e3232; }}
"""

# kind -> (badge colour, glyph)
KINDS = {
    "question": (theme.ACCENT, "?"),
    "warning": ("#ffb74d", "!"),
    "danger": (theme.DANGER, "!"),
    "info": ("#33CCFF", "i"),
}


class _Badge(QWidget):
    def __init__(self, color: str, glyph: str):
        super().__init__()
        self.color, self.glyph = QColor(color), glyph
        self.setFixedSize(40, 40)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        c = QColor(self.color)
        c.setAlpha(46)
        p.setBrush(c)
        p.drawEllipse(QRectF(0, 0, 40, 40))
        p.setPen(self.color)
        f = QFont("Segoe UI", 15)
        f.setWeight(QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.glyph)


class StickDialog(QDialog):
    """Title + message + two buttons, in StickAI's dark style.
    Enter = the highlighted (accept) button, Esc / ✕ = reject."""

    def __init__(self, parent, title: str, text: str, accept: str, reject: str,
                 kind: str = "question", accept_default: bool = True):
        super().__init__(parent)
        self.setObjectName("StickDialog")
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setStyleSheet(STYLE)
        if parent is not None and not parent.windowIcon().isNull():
            self.setWindowIcon(parent.windowIcon())

        color, glyph = KINDS.get(kind, KINDS["question"])
        head = QLabel(title)
        head.setObjectName("DlgTitle")
        body = QLabel(text)
        body.setObjectName("DlgText")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setMinimumWidth(320)
        body.setMaximumWidth(460)

        texts = QVBoxLayout()
        texts.setSpacing(6)
        texts.addWidget(head)
        texts.addWidget(body)
        top = QHBoxLayout()
        top.setSpacing(14)
        top.addWidget(_Badge(color, glyph), 0, Qt.AlignmentFlag.AlignTop)
        top.addLayout(texts, 1)

        self.accept_btn = QPushButton(accept)
        self.accept_btn.setObjectName("Danger" if kind == "danger" else "Primary")
        self.reject_btn = QPushButton(reject)
        self.accept_btn.clicked.connect(self.accept)
        self.reject_btn.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(self.reject_btn)
        btns.addWidget(self.accept_btn)
        default = self.accept_btn if accept_default else self.reject_btn
        default.setDefault(True)
        default.setAutoDefault(True)
        (self.reject_btn if accept_default else self.accept_btn).setAutoDefault(False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(18)
        lay.addLayout(top)
        lay.addLayout(btns)
        self.setFixedSize(self.sizeHint())
        default.setFocus()

    def showEvent(self, e) -> None:
        theme.dark_title_bar(self)
        super().showEvent(e)
        owner = self.parentWidget()
        theme.keep_on_screen(self)
        if owner is not None and owner.isVisible():  # centre over the owner, kept on its screen
            geo = owner.screen().availableGeometry()
            c = owner.frameGeometry().center()
            x = min(max(c.x() - self.width() // 2, geo.left() + 8), geo.right() - self.width() - 8)
            y = min(max(c.y() - self.height() // 2, geo.top() + 8), geo.bottom() - self.height() - 8)
            self.move(x, y)


def confirm(parent, title: str, text: str, accept: str, reject: str, kind: str = "question",
            accept_default: bool = True) -> bool:
    """Blocking yes/no. Returns True if the accept button was pressed."""
    dlg = StickDialog(parent, title, text, accept, reject, kind, accept_default)
    try:
        return int(dlg.exec()) == _ACCEPTED
    finally:
        dlg.deleteLater()


def ask_async(parent, title: str, text: str, accept: str, reject: str,
              done: Callable[[bool], None], kind: str = "question") -> StickDialog:
    """Non-blocking (window-modal) yes/no; calls done(True/False) once."""
    dlg = StickDialog(parent, title, text, accept, reject, kind)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)

    def finished(result: int) -> None:
        dlg.deleteLater()
        done(int(result) == _ACCEPTED)

    dlg.finished.connect(finished)
    dlg.open()
    return dlg
