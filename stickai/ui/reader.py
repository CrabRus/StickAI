"""A window with a document's text: exactly what's written (read from the file, not a
screenshot), laid out (headings, paragraphs, samples), ready to copy or translate.

Translation goes through Gemini only when you click "Translate" (or asked for it in the chat).
"""
from __future__ import annotations

import os
import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QTextDocument
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from ..brain.characters import LANGUAGES
from ..i18n import tr
from . import theme

STYLE = f"""
QWidget#Reader {{ background: {theme.BG}; }}
QLabel {{ color: {theme.MUTED}; font: 9pt 'Segoe UI'; }}
QLabel#Title {{ color: {theme.TEXT}; font: 600 11pt 'Segoe UI'; }}
QTextBrowser {{ background: {theme.SURFACE}; color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 10px;
               padding: 14px 16px; font: 10.5pt 'Segoe UI'; selection-background-color: {theme.ACCENT};
               selection-color: #1b1b1b; }}
QPushButton {{ background: {theme.FIELD}; color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 8px;
               padding: 6px 12px; font: 9.5pt 'Segoe UI'; }}
QPushButton:hover {{ background: #343944; }}
QPushButton#Primary {{ background: {theme.ACCENT}; color: #1b1b1b; border: none; font-weight: 600; }}
QPushButton:disabled {{ color: {theme.MUTED}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: {theme.BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""
CSS = """
h1 { font-size: 15pt; color: #ffffff; } h3 { font-size: 12.5pt; color: #FEC503; margin-top: 14px; }
pre, code { font-family: Consolas, 'Cascadia Mono', monospace; font-size: 10pt; background: #1d2026; }
p { line-height: 140%; }
"""


class ReaderWindow(QWidget):
    def __init__(self, app, title: str, markdown: str, source: str = "", jump_to: str = "", translate: bool = False):
        super().__init__(None, Qt.WindowType.Window)
        self.app = app
        self.setObjectName("Reader")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet(STYLE)
        self.setWindowTitle(f"{title} — StickAI")
        theme.fit_window(self, 720, 820, min_w=360, min_h=320)
        self.original = markdown
        self.source = source
        self._stream_id = None
        self._parts: list[str] = []

        head = QLabel(title)
        head.setObjectName("Title")
        self.status = QLabel(tr("rd.exact"))
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.document().setDefaultStyleSheet(CSS)
        self.view.setMarkdown(markdown)

        copy = QPushButton(tr("rd.copy"))
        copy.clicked.connect(self._copy)
        lang = LANGUAGES.get(app.roster.settings.language, "English")
        self.translate_btn = QPushButton(tr("rd.translate"))
        self.translate_btn.setObjectName("Primary")
        self.translate_btn.setToolTip(tr("rd.translate_tip", lang=lang))
        self.translate_btn.clicked.connect(self.translate)
        self.original_btn = QPushButton(tr("rd.original"))
        self.original_btn.clicked.connect(self._show_original)
        self.original_btn.hide()
        buttons = QHBoxLayout()
        buttons.addWidget(copy)
        buttons.addWidget(self.translate_btn)
        buttons.addWidget(self.original_btn)
        if source and os.path.isfile(source):
            open_btn = QPushButton(tr("rd.open_file"))
            open_btn.clicked.connect(lambda: os.startfile(source))
            buttons.addWidget(open_btn)
        buttons.addStretch(1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 14)
        lay.setSpacing(8)
        lay.addWidget(head)
        lay.addWidget(self.status)
        lay.addWidget(self.view, 1)
        lay.addLayout(buttons)
        self.jump_to = jump_to
        if translate:
            QTimer.singleShot(0, self.translate)

    def open(self) -> None:
        geo = QGuiApplication.primaryScreen().availableGeometry()
        self.move(geo.left() + (geo.width() - self.width()) // 2, geo.top() + 16)
        self.show()
        theme.keep_on_screen(self)
        self.raise_()
        self.activateWindow()
        if self.jump_to:
            self._jump(self.jump_to)

    def _jump(self, text: str) -> None:
        """Scroll to (and select) the part asked for, e.g. "6.1"."""
        doc = self.view.document()
        cur = doc.find(text, 0, QTextDocument.FindFlag(0))
        if cur.isNull():
            return
        self.view.setTextCursor(cur)
        self.view.ensureCursorVisible()
        bar = self.view.verticalScrollBar()
        bar.setValue(max(0, bar.value() + self.view.cursorRect().top() - 40))

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.view.toPlainText())
        self.status.setText(tr("rd.copied"))

    def _show_original(self) -> None:
        self.view.setMarkdown(self.original)
        self.original_btn.hide()
        self.translate_btn.show()
        self.status.setText(tr("rd.exact"))

    # ---- translation (Gemini, only on request)
    def _looks_translated(self, text: str) -> bool:
        """Small models sometimes hand the original back: check the result really is in the target language."""
        lang_code = self.app.roster.settings.language
        letters = [c for c in text if c.isalpha()]
        if not letters:
            return False
        if lang_code == "ru":
            return sum("а" <= c.lower() <= "я" or c in "ёЁ" for c in letters) / len(letters) > 0.4
        words = lambda t: {w.lower() for w in re.findall(r"\w{4,}", t)}
        a, b = words(self.original), words(text)
        return bool(b) and len(a & b) / len(b) < 0.5  # mostly different words from the original

    def translate(self, retry: bool = False) -> None:
        helper = getattr(self.app, "code", None)
        if helper is None or self._stream_id is not None:
            return
        self._retry = retry
        key = helper.coder()
        if not helper.stream.has_key(key):
            self.status.setText(tr("err.no_key"))
            return
        lang = LANGUAGES.get(self.app.roster.settings.language, "English")
        system = (f"You are a translator. Translate the user's document into {lang}. The output must be in {lang}: "
                  "every heading, label and sentence translated (do NOT repeat the original language). Keep the "
                  "Markdown structure (headings, bold labels, code blocks); inside code blocks change nothing (it's "
                  "program input / output). Keep names, numbers and code identifiers as plain text (no LaTeX, no $). No "
                  "summary, no notes, no "
                  "preface: only the translated document.")
        self.translate_btn.setEnabled(False)
        self.status.setText(tr("rd.translating", lang=lang))
        self._parts = []
        stream = helper.stream
        stream.chunk.connect(self._on_chunk)
        stream.finished.connect(self._on_done)
        models = tuple(sorted(self.app.roster.settings.models, key=lambda m: "lite" in m))
        nl = chr(10)
        if retry:
            system = (f"IMPORTANT: a previous attempt returned the text in its ORIGINAL language. Your answer must be "
                      f"written in {lang}. ") + system
        prompt = f"Translate into {lang}:{nl}{nl}{self.original[:40000]}{nl}{nl}(Reply with the {lang} translation only.)"
        self._stream_id = stream.start(models, system, prompt, 0.2, owner=key)

    def _on_chunk(self, sid: int, text: str) -> None:
        if sid != self._stream_id:
            return
        self._parts.append(text)
        if len(self._parts) % 3 == 1:  # re-render now and then, not on every piece
            self._render_translation()

    def _render_translation(self) -> None:
        bar = self.view.verticalScrollBar()
        pos = bar.value()
        self.view.setMarkdown("".join(self._parts))
        bar.setValue(pos)

    def _on_done(self, sid: int, err: str, _model: str) -> None:
        if sid != self._stream_id:
            return
        stream = self.app.code.stream
        stream.chunk.disconnect(self._on_chunk)
        stream.finished.disconnect(self._on_done)
        self._stream_id = None
        self.translate_btn.setEnabled(True)
        if err or not self._parts:
            self.status.setText(self.app.code._error_text(err) if err else tr("rd.failed"))
            return
        if not self._looks_translated("".join(self._parts)):
            if not getattr(self, "_retry", False):
                self.translate(retry=True)  # one more try, told plainly
                return
            self.view.setMarkdown(self.original)
            self.status.setText(tr("rd.failed"))
            return
        self._render_translation()
        self.translate_btn.hide()
        self.original_btn.show()
        self.status.setText(tr("rd.translated"))

    def closeEvent(self, e) -> None:
        if self._stream_id is not None:
            self.app.code.stream.cancel(self._stream_id)
            try:
                self.app.code.stream.chunk.disconnect(self._on_chunk)
                self.app.code.stream.finished.disconnect(self._on_done)
            except (RuntimeError, TypeError):
                pass
        super().closeEvent(e)
