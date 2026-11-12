"""Chat window: the conversation between you and the stickmen.

* shows the whole history (including earlier sessions) with each character's
  messages in their own colour,
* send text with Enter (Shift+Enter = new line),
* send pictures: the clip button, Ctrl+V with an image on the clipboard, or
  drag an image file onto the window.
"""
from __future__ import annotations

import ctypes
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDateTime, QEvent, QRectF, QLocale, QMimeData, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QSizePolicy, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QScrollArea, QToolButton,
    QVBoxLayout, QWidget,
)

from ..brain.memory import Entry
from ..i18n import language, tr
from . import theme
from . import spellcheck

IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp)"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
BUBBLE_MAX_W = 300
THUMB = 220

STYLE = """
QWidget#ChatWindow { background: #1d2026; }
QScrollArea, QWidget#Messages { background: #1d2026; border: none; }
QLabel { color: #e9e9ea; font: 10.5pt 'Segoe UI'; }
QLabel#Meta { color: #8d929c; font: 8.5pt 'Segoe UI'; }
QLabel#Event { color: #8d929c; font: italic 8.5pt 'Segoe UI'; }
QLabel#Status { color: #a8adb7; font: 9pt 'Segoe UI'; padding: 0 4px; }
QLabel#Present { color: #c9ccd2; font: 9pt 'Segoe UI'; }
QPlainTextEdit { background: #2a2e36; color: #f2f2f2; border: 1px solid #3a3f4a; border-radius: 10px;
                 padding: 7px 8px; font: 10.5pt 'Segoe UI'; selection-background-color: #FEC503; }
QPlainTextEdit:focus { border: 1px solid #6b7180; }
QPushButton#Send { background: #FEC503; color: #1b1b1b; border: none; border-radius: 10px;
                   font: bold 10pt 'Segoe UI'; padding: 0 16px; }
QPushButton#Send:hover { background: #ffd43b; }
QToolButton { background: #2a2e36; color: #e9e9ea; border: 1px solid #3a3f4a; border-radius: 10px;
              font: 13pt 'Segoe UI Emoji'; padding: 4px 8px; }
QToolButton#Row { padding: 0; }
QToolButton:hover { background: #343944; }
QToolButton#Quit { color: #ff8a80; }
QToolButton#Quit:hover { background: #5a2a2a; }
QFrame#Attach { background: #2a2e36; border-radius: 8px; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: #3a3f4a; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def _text_color(hex_color: str) -> str:
    c = QColor(hex_color)
    lum = 0.2126 * c.redF() ** 2.2 + 0.7152 * c.greenF() ** 2.2 + 0.0722 * c.blueF() ** 2.2
    return "#141418" if lum > 0.22 else "#ffffff"


def _image_from_mime(md: QMimeData) -> QImage | None:
    if md.hasImage():
        img = QImage(md.imageData())
        if not img.isNull():
            return img
    for url in md.urls():
        p = Path(url.toLocalFile())
        if p.suffix.lower() in IMAGE_EXT and p.exists():
            img = QImage(str(p))
            if not img.isNull():
                return img
    return None


class MessageBubble(QWidget):
    """A chat message: rounded rectangle + wrapped text, painted directly.

    (A QLabel with its own stylesheet per message made Qt re-run the style
    engine for every bubble on each scroll/resize, which is what made the chat
    window feel sluggish with a long history.)"""

    PAD_X, PAD_Y, RADIUS = 11, 7, 12

    def __init__(self, text: str, bg: str, fg: str, bold: bool):
        super().__init__()
        self.text, self.bg, self.fg = text, QColor(bg), QColor(fg)
        self.font_ = QFont("Segoe UI", 10)
        self.font_.setPointSizeF(10.5)
        if bold:
            self.font_.setWeight(QFont.Weight.DemiBold)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.setToolTip("")
        self._cache_w = -1
        self._cache_size = QSize()

    def _text_size(self, max_w: int) -> QSize:
        if max_w != self._cache_w:
            r = QFontMetrics(self.font_).boundingRect(0, 0, max_w, 100000, Qt.TextFlag.TextWordWrap, self.text)
            self._cache_w, self._cache_size = max_w, QSize(r.width() + 2, r.height())
        return self._cache_size

    def sizeHint(self) -> QSize:
        t = self._text_size(BUBBLE_MAX_W - 2 * self.PAD_X)
        return QSize(t.width() + 2 * self.PAD_X, t.height() + 2 * self.PAD_Y)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.bg)
        p.drawRoundedRect(QRectF(self.rect()), self.RADIUS, self.RADIUS)
        p.setPen(self.fg)
        p.setFont(self.font_)
        p.drawText(self.rect().adjusted(self.PAD_X, self.PAD_Y, -self.PAD_X, -self.PAD_Y),
                   Qt.TextFlag.TextWordWrap, self.text)

    def contextMenuEvent(self, e) -> None:  # right-click: copy the message
        QGuiApplication.clipboard().setText(self.text)


ROW_H = 40       # height of the input row: buttons and a one-line input box line up
MAX_INPUT_LINES = 6


class ChatInput(QPlainTextEdit):
    """One line high like the buttons next to it; grows with the text (up to a few lines)."""
    send = Signal()
    image_pasted = Signal(QImage)

    def __init__(self):
        super().__init__()
        self.setTabChangesFocus(True)
        # No scrollbar ever: one appearing / disappearing changes the width, which re-wraps the text,
        # which changes the height... a feedback loop that froze the chat while typing. The box still
        # scrolls to the cursor (and with the wheel) past MAX_INPUT_LINES.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(2)
        self._fit_pending = False
        self.document().documentLayout().documentSizeChanged.connect(lambda _s: self._fit_later())
        self.setFixedHeight(ROW_H)
        self.spell = spellcheck.SpellHighlighter(self.document(), spellcheck.SpellChecker())

    def contextMenuEvent(self, e) -> None:
        menu = self.createStandardContextMenu(e.pos())
        spellcheck.add_actions(menu, self, self.spell, e.pos(),
                               {"add": tr("spell.add"), "ignore": tr("spell.ignore"), "none": tr("spell.none")})
        menu.setStyleSheet(theme.MENU_STYLE)
        menu.exec(e.globalPos())
        menu.deleteLater()

    def _fit_later(self) -> None:
        """Coalesce: many size changes in one burst (pasting, typing fast) -> one fit, after layout."""
        if not self._fit_pending:
            self._fit_pending = True
            QTimer.singleShot(0, self._fit)

    def _fit(self) -> None:
        self._fit_pending = False
        lines = max(1, min(MAX_INPUT_LINES, int(self.document().documentLayout().documentSize().height())))
        extra = ROW_H - self.fontMetrics().lineSpacing()  # frame + padding + margins around one line
        want = max(ROW_H, lines * self.fontMetrics().lineSpacing() + extra)
        if want != self.height():  # resizing relayouts the whole chat window: only when it really changes
            self.setFixedHeight(want)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if e.oldSize().width() != e.size().width():
            self._fit_later()  # a narrower window wraps more lines (a height change can't loop back here)

    def set_spellcheck(self, on: bool, names=()) -> None:
        """Turn the red underlines on/off; never flag the characters' names."""
        self.spell.checker.ignored.update(n.lower() for n in names)
        self.spell.set_enabled(on)

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.send.emit()
            return
        super().keyPressEvent(e)

    def canInsertFromMimeData(self, md) -> bool:
        return _image_from_mime(md) is not None or super().canInsertFromMimeData(md)

    def insertFromMimeData(self, md) -> None:
        img = _image_from_mime(md)
        if img is not None:
            self.image_pasted.emit(img)
        else:
            super().insertFromMimeData(md)


class ChatWindow(QWidget):
    submitted = Signal(str, object)  # text, QImage | None
    settings_requested = Signal()
    quit_requested = Signal()
    look_requested = Signal()

    def __init__(self, app):
        super().__init__(None, Qt.WindowType.Window)
        self.app = app
        self.setObjectName("ChatWindow")
        self.setStyleSheet(STYLE)
        self.setAcceptDrops(True)
        self.resize(440, 640)
        self._attached: QImage | None = None
        self._last_session = ""
        self._last_char = ""

        # header: who is on the desktop
        self.present = QLabel()
        self.present.setObjectName("Present")
        self.present.setTextFormat(Qt.TextFormat.RichText)
        self.present.setWordWrap(True)
        self.gear = QToolButton()
        self.gear.setText("⚙")
        self.gear.clicked.connect(self.settings_requested.emit)
        header = QHBoxLayout()
        header.addWidget(self.present, 1)
        header.addWidget(self.gear, 0, Qt.AlignmentFlag.AlignTop)
        self.quit_btn = QToolButton()
        self.quit_btn.setObjectName("Quit")
        self.quit_btn.setText("⏻")
        self.quit_btn.clicked.connect(self.quit_requested.emit)
        header.addWidget(self.quit_btn, 0, Qt.AlignmentFlag.AlignTop)

        # messages
        self.messages = QWidget()
        self.messages.setObjectName("Messages")
        self.mlay = QVBoxLayout(self.messages)
        self.mlay.setContentsMargins(10, 10, 10, 10)
        self.mlay.setSpacing(6)
        self.mlay.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.messages)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Stay pinned to the newest message unless the user scrolled up to read.
        self._stick = True
        sb = self.scroll.verticalScrollBar()
        sb.valueChanged.connect(lambda v: setattr(self, "_stick", v >= sb.maximum() - 40))
        sb.rangeChanged.connect(lambda _lo, hi: sb.setValue(hi) if self._stick else None)

        # attachment preview
        self.attach_frame = QFrame()
        self.attach_frame.setObjectName("Attach")
        al = QHBoxLayout(self.attach_frame)
        al.setContentsMargins(6, 6, 6, 6)
        self.attach_thumb = QLabel()
        self.attach_label = QLabel()
        self.attach_label.setObjectName("Meta")
        self.rm = QToolButton()
        self.rm.setText("✕")
        self.rm.clicked.connect(lambda: self.set_attachment(None))
        al.addWidget(self.attach_thumb)
        al.addWidget(self.attach_label, 1)
        al.addWidget(self.rm)
        self.attach_frame.hide()

        # input row
        self.input = ChatInput()
        self.input.send.connect(self._send)
        self.input.image_pasted.connect(self.set_attachment)
        self.look_btn = QToolButton()
        self.look_btn.setText("👁")
        self.look_btn.clicked.connect(self.look_requested.emit)
        self.clip = QToolButton()
        self.clip.setText("📎")
        self.clip.clicked.connect(self._pick_image)
        self.send_btn = QPushButton()
        self.send_btn.setObjectName("Send")
        self.send_btn.clicked.connect(self._send)
        for b in (self.look_btn, self.clip):
            b.setObjectName("Row")
            b.setFixedSize(ROW_H + 6, ROW_H)
        self.send_btn.setFixedHeight(ROW_H)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.clip, 0, Qt.AlignmentFlag.AlignBottom)
        row.addWidget(self.look_btn, 0, Qt.AlignmentFlag.AlignBottom)
        row.addWidget(self.input, 1)
        row.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)

        self.status = QLabel()
        self.status.setObjectName("Status")
        self.status.setWordWrap(True)
        self.status.hide()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(6)
        lay.addLayout(header)
        lay.addWidget(self.scroll, 1)
        lay.addWidget(self.attach_frame)
        lay.addLayout(row)
        lay.addWidget(self.status)
        self._people: list[tuple[str, str]] = []
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(tr("chat.title"))
        self.send_btn.setText(tr("chat.send"))
        self.clip.setToolTip(tr("chat.attach_tip"))
        self.look_btn.setToolTip(tr("chat.look_tip"))
        self.rm.setToolTip(tr("chat.remove_pic"))
        self.gear.setToolTip(tr("chat.settings_tip"))
        self.quit_btn.setToolTip(tr("chat.quit_tip"))
        self.set_present(self._people)

    # ------------------------------------------------------------ window
    def showEvent(self, e) -> None:
        theme.dark_title_bar(self)  # re-applied: changing window flags recreates the native window
        super().showEvent(e)

    def changeEvent(self, e) -> None:
        # The title-bar "minimize" hides the chat like the X does: a minimized chat would stay in
        # the taskbar and Alt+` would "show" it while it's still minimized (invisible).
        super().changeEvent(e)
        if e.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self._hide_unminimized)

    def _hide_unminimized(self) -> None:
        self.hide()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)

    def apply_on_top(self, on_top: bool) -> None:
        was = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on_top)
        if was:
            self.show()

    def toggle(self) -> None:
        if self.isVisible() and self.isActiveWindow() and not self.isMinimized():
            self.hide()
        else:
            self.summon()

    def summon(self) -> None:
        if not self.isVisible():
            geo = QGuiApplication.primaryScreen().availableGeometry()
            if not getattr(self, "_placed", False):
                self.move(geo.right() - self.width() - 24, geo.bottom() - self.height() - 24)
                self._placed = True
        if self.isMinimized():  # minimized with the title-bar button: un-minimize, don't just "show"
            self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
            ctypes.windll.user32.ShowWindow(int(self.winId()), 9)  # SW_RESTORE
        self.show()
        self.raise_()
        self.activateWindow()
        ctypes.windll.user32.SetForegroundWindow(int(self.winId()))  # allowed right after our hotkey
        self.input.setFocus()
        QTimer.singleShot(0, self._scroll_to_end)

    def closeEvent(self, e) -> None:
        # The title-bar X (a spontaneous close) just hides the chat. Closes sent
        # by the app itself while quitting must be accepted, otherwise Qt
        # cancels the whole quit.
        if e.spontaneous():
            e.ignore()
            self.hide()
        else:
            e.accept()

    # --------------------------------------------------------- messages
    def set_present(self, people: list[tuple[str, str]]) -> None:
        self._people = list(people)
        if not people:
            self.present.setText(tr("chat.nobody"))
            return
        dots = "&nbsp;&nbsp;".join(f'<span style="color:{c}">●</span>&nbsp;{n}' for n, c in people)
        self.present.setText(f"{tr('chat.present')}&nbsp;&nbsp;{dots}")

    def set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def add_entry(self, e: Entry, follow: bool = True) -> None:
        if e.session != self._last_session:
            self._last_session = e.session
            self._last_char = ""
            when = QLocale({"ru": "ru_RU", "sk": "sk_SK"}.get(language(), "en_GB")).toString(
                QDateTime.fromSecsSinceEpoch(int(e.ts)), "ddd d MMM, HH:mm")
            sep = QLabel(f"──────  {when}  ──────")
            sep.setObjectName("Meta")
            sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._append(sep)

        if e.kind == "event":
            lab = QLabel(e.text)
            lab.setObjectName("Event")
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setWordWrap(True)
            self._append(lab)
            self._last_char = ""
        elif e.kind == "user":
            self._append(self._bubble_row(e.text, "#3b4150", "#f2f2f2", right=True,
                                          image=e.image, meta=None))
            self._last_char = ""
        elif e.kind == "char":
            spec = self.app.roster.characters.get(e.speaker)
            color = spec.color if spec else "#888888"
            name = spec.display_name if spec else e.speaker
            meta = None
            if e.speaker != self._last_char or (e.to and e.to not in ("user", "everyone")):
                to = ""
                if e.to and e.to not in ("user", "everyone"):
                    t = self.app.roster.characters.get(e.to)
                    to = f"  →  {t.display_name if t else e.to}"
                meta = f'<span style="color:{color}; font-weight:600">{name}</span>' \
                       f'<span style="color:#8d929c">{to}</span>'
            self._append(self._bubble_row(e.text, color, _text_color(color), right=False, meta=meta))
            self._last_char = e.speaker

    def _bubble_row(self, text, bg, fg, right, image="", meta=None) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        def place(widget):
            # Side alignment via a stretch, not an alignment flag: word-wrapped
            # labels only get their full height (heightForWidth) this way.
            h = QHBoxLayout()
            h.setContentsMargins(0, 0, 0, 0)
            if right:
                h.addStretch(1)
            h.addWidget(widget)
            if not right:
                h.addStretch(1)
            v.addLayout(h)

        if meta:
            m = QLabel(meta)
            m.setTextFormat(Qt.TextFormat.RichText)
            m.setObjectName("Meta")
            place(m)
        if image:
            path = self.app.memory.root / image
            pm = QPixmap(str(path))
            if not pm.isNull():
                thumb = QLabel()
                thumb.setPixmap(pm.scaled(THUMB, THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
                thumb.setCursor(Qt.CursorShape.PointingHandCursor)
                thumb.setToolTip(tr("chat.open_pic"))
                thumb.mousePressEvent = lambda _e, p=path: os.startfile(p)
                place(thumb)
        if text:
            place(MessageBubble(text, bg, fg, bold=not right))
        return w

    def clear_messages(self) -> None:
        while self.mlay.count() > 1:  # keep the top stretch
            item = self.mlay.takeAt(1)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._last_session = ""
        self._last_char = ""

    def _append(self, w: QWidget) -> None:
        self.mlay.addWidget(w)

    def _scroll_to_end(self) -> None:
        self._stick = True
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ------------------------------------------------------------ input
    def set_attachment(self, img: QImage | None) -> None:
        self._attached = img if img is not None and not img.isNull() else None
        if self._attached is None:
            self.attach_frame.hide()
            return
        self.attach_thumb.setPixmap(QPixmap.fromImage(self._attached).scaled(
            QSize(56, 56), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.attach_label.setText(tr("chat.pic_attached", w=self._attached.width(), h=self._attached.height()))
        self.attach_frame.show()
        self.input.setFocus()

    def _pick_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("chat.pick_pic"), str(Path.home() / "Pictures"), IMAGE_FILTER)
        if path:
            self.set_attachment(QImage(path))

    def dragEnterEvent(self, e) -> None:
        if _image_from_mime(e.mimeData()) is not None:
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        img = _image_from_mime(e.mimeData())
        if img is not None:
            self.set_attachment(img)
            e.acceptProposedAction()

    def _send(self) -> None:
        text = self.input.toPlainText().strip()
        if not text and self._attached is None:
            return
        img = self._attached
        self.input.clear()
        self.set_attachment(None)
        self.submitted.emit(text, img)
