"""Settings window (⚙ in the chat window, or the tray menu).

Edits a draft copy of the roster; Save writes characters.toml and applies it
to the running app (stickmen change colour/size on the spot).
"""
from __future__ import annotations

import os
from pathlib import Path
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QFileDialog, QSpinBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QScrollArea,
    QSlider, QTabWidget, QVBoxLayout, QWidget,
)

from ..brain.characters import BUBBLE_MODES, LANGUAGES, VISION_MODES, Roster, load_defaults, save_roster
from ..i18n import tr
from ..tools.files import default_folder
from ..brain.gemini import character_keys, save_character_keys
from .hotkey import parse_hotkey
from . import theme
from .dialogs import confirm

LANG_LABELS = {"en": "English", "ru": "Русский", "sk": "Slovenčina"}
BRAINS = {  # model lists tried in order (the next one is used if a model is unavailable / busy)
    "fast": ["gemini-3.5-flash-lite", "gemini-3.8-flash"],
    "smart": ["gemini-3.8-flash", "gemini-3.5-flash-lite"],
    "pro": ["gemini-3.1-pro-preview", "gemini-3.8-flash", "gemini-3.5-flash-lite"],
}

STYLE = """
QDialog, QWidget#Page, QScrollArea, QWidget#Form { background: #1d2026; }
QLabel { color: #e1e2e5; font: 10pt 'Segoe UI'; }
QLabel#Hint { color: #8d929c; font: 9pt 'Segoe UI'; }
QLabel#Section { color: #ffffff; font: 600 11pt 'Segoe UI'; padding-top: 6px; }
QLabel#Status { color: #a8adb7; font: 9pt 'Segoe UI'; }
QLabel#Memory { background: #252932; color: #cfd2d8; border-radius: 8px; padding: 8px 10px; font: 10pt 'Segoe UI'; }
QTabWidget::pane { border: 1px solid #2f343e; border-radius: 8px; top: -1px; }
QTabBar::tab { background: #252932; color: #b7bbc4; padding: 7px 16px; border-top-left-radius: 8px;
               border-top-right-radius: 8px; margin-right: 2px; font: 10pt 'Segoe UI'; }
QTabBar::tab:selected { background: #2f343e; color: #ffffff; }
QLineEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox, QSpinBox {
    background: #2a2e36; color: #f2f2f2; border: 1px solid #3a3f4a; border-radius: 8px;
    padding: 5px 8px; font: 10pt 'Segoe UI'; selection-background-color: #FEC503; selection-color: #111; }
QPlainTextEdit#Long { font: 11pt 'Segoe UI'; padding: 8px 10px; }
QComboBox QAbstractItemView { background: #2a2e36; color: #f2f2f2; selection-background-color: #3b4150; }
QCheckBox { color: #e1e2e5; font: 10pt 'Segoe UI'; spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #4a505c; border-radius: 4px; background: #2a2e36; }
QCheckBox::indicator:hover { border: 1px solid #6b7180; }
QCheckBox::indicator:checked { background: #FEC503; border: 1px solid #FEC503; image: url(CHECK_SVG); }
QCheckBox::indicator:disabled { background: #23262d; border: 1px solid #33373f; }
QCheckBox::indicator:checked:disabled { background: #6e5a1c; border: 1px solid #6e5a1c; }
QCheckBox:disabled { color: #6b7180; }
QListWidget { background: #252932; color: #e9e9ea; border: none; border-radius: 8px; font: 11pt 'Segoe UI';
              padding: 4px; }
QListWidget::item { padding: 8px 6px; border-radius: 6px; }
QListWidget::item:selected { background: #3b4150; color: #ffffff; }
QPushButton { background: #2f343e; color: #e9e9ea; border: 1px solid #3a3f4a; border-radius: 8px;
              padding: 6px 14px; font: 10pt 'Segoe UI'; }
QPushButton:hover { background: #3a3f4a; }
QPushButton#Primary { background: #FEC503; color: #1b1b1b; border: none; font: bold 10pt 'Segoe UI'; }
QPushButton#Primary:hover { background: #ffd43b; }
QPushButton#Danger { color: #ff8a80; }
QSlider::groove:horizontal { height: 4px; background: #3a3f4a; border-radius: 2px; }
QSlider::handle:horizontal { width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; background: #FEC503; }
QToolButton { background: #2a2e36; color: #e9e9ea; border: 1px solid #3a3f4a; border-radius: 8px; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: #3a3f4a; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def _dot(color: str, size: int = 14) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


def _glyph(text: str, size: int = 32) -> QIcon:
    """An emoji drawn into an icon (QLineEdit actions show icons, not text)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setFont(QFont("Segoe UI Emoji", int(size * 0.55)))
    p.setPen(QColor("#c9ccd2"))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return QIcon(pm)


class _SliderBox(QWidget):
    """Slider + number box bound together (for 0..1 / 0..2 style values)."""

    def __init__(self, lo: float, hi: float, step: float, suffix: str = "", percent: bool = False):
        super().__init__()
        self.percent = percent
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(int(lo / step), int(round(hi / step)))
        self.step = step
        self.box = QDoubleSpinBox()
        self.box.setRange(lo * (100 if percent else 1), hi * (100 if percent else 1))
        self.box.setSingleStep(step * (100 if percent else 1))
        self.box.setDecimals(0 if percent else 2)
        self.box.setSuffix(suffix)
        self.box.setFixedWidth(92)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.box)
        self.slider.valueChanged.connect(lambda v: self.box.setValue(v * step * (100 if percent else 1)))
        self.box.valueChanged.connect(lambda v: self.slider.setValue(int(round(v / (100 if percent else 1) / step))))

    def value(self) -> float:
        return self.box.value() / (100 if self.percent else 1)

    def set_value(self, v: float) -> None:
        self.box.setValue(v * (100 if self.percent else 1))


class SettingsDialog(QDialog):
    def __init__(self, app):
        # Owned by the chat window: Windows keeps an owned window above its
        # owner (the chat is always-on-top), and application-modal blocks the
        # chat and the rest of the app until Settings is closed.
        super().__init__(app.chat)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.app = app
        self.draft: Roster = app.roster
        self.current: str | None = None
        self._loading = False
        self.setWindowTitle(tr("set.title"))
        self.setWindowIcon(app.tray.icon())
        self.setStyleSheet(STYLE.replace("CHECK_SVG", (Path(__file__).parent / "check.svg").as_posix()))
        self.resize(780, 680)

        tabs = QTabWidget()
        general = QScrollArea()  # scrolls on small screens
        general.setWidgetResizable(True)
        general.setWidget(self._general_page())
        tabs.addTab(general, tr("set.tab_general"))
        tabs.addTab(self._characters_page(), tr("set.tab_chars"))
        tabs.addTab(self._memory_page(), tr("set.tab_memory"))
        self.tabs = tabs

        self.status = QLabel()
        self.status.setObjectName("Status")
        open_file = QPushButton(tr("set.open_file"))
        open_file.clicked.connect(lambda: os.startfile(self.app.characters_path))
        revert = QPushButton(tr("set.revert"))
        revert.clicked.connect(self.load_from_app)
        save = QPushButton(tr("set.save"))
        save.setObjectName("Primary")
        save.clicked.connect(self.save)
        close = QPushButton(tr("set.close"))
        close.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addWidget(open_file)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(revert)
        bottom.addWidget(save)
        bottom.addWidget(close)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.addWidget(tabs, 1)
        lay.addLayout(bottom)
        self.load_from_app()

    # ================================================================ pages
    def _general_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Page")
        form = QFormLayout(page)
        form.setContentsMargins(16, 16, 16, 16)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.lang = QComboBox()
        for code in LANGUAGES:
            self.lang.addItem(LANG_LABELS[code], code)
        hint = QLabel(tr("set.language_hint"))
        hint.setObjectName("Hint")
        form.addRow(tr("set.language"), self.lang)
        form.addRow("", hint)

        self.bubbles = QComboBox()
        for mode in BUBBLE_MODES:
            self.bubbles.addItem(tr(f"set.bubbles_{mode}"), mode)
        form.addRow(tr("set.bubbles"), self.bubbles)
        self.vision = QComboBox()
        for mode in VISION_MODES:
            self.vision.addItem(tr(f"set.vision_{mode}"), mode)
        vhint = QLabel(tr("set.vision_hint"))
        vhint.setObjectName("Hint")
        vhint.setWordWrap(True)
        form.addRow(tr("set.vision"), self.vision)
        form.addRow("", vhint)

        self.banter = QCheckBox(tr("set.banter"))
        self.b_lo = QDoubleSpinBox()
        self.b_hi = QDoubleSpinBox()
        for b in (self.b_lo, self.b_hi):
            b.setRange(0.5, 120)
            b.setSingleStep(0.5)
            b.setDecimals(1)
            b.setFixedWidth(84)
        rng = QHBoxLayout()
        rng.addWidget(self.b_lo)
        rng.addWidget(QLabel("–"))
        rng.addWidget(self.b_hi)
        rng.addStretch(1)
        form.addRow("", self.banter)
        form.addRow(tr("set.banter_range"), rng)

        self.react = QCheckBox(tr("set.react"))
        self.greet = QCheckBox(tr("set.greet"))
        self.widgets = QCheckBox(tr("set.widgets"))
        self.web = QCheckBox(tr("set.web"))
        self.control = QCheckBox(tr("set.control"))
        self.trust = QCheckBox(tr("set.trust"))
        self.files = QCheckBox(tr("set.files"))
        self.trust_files = QCheckBox(tr("set.trust_files"))
        self.trust_files.setStyleSheet("margin-left: 22px;")
        self.files.toggled.connect(self.trust_files.setEnabled)
        self.files_folder = QLineEdit()
        self.files_folder.setPlaceholderText(default_folder())
        browse = QPushButton(tr("set.browse"))
        browse.clicked.connect(self._browse_folder)
        self.files.toggled.connect(lambda on: (self.files_folder.setEnabled(on), browse.setEnabled(on)))
        self._folder_row = QHBoxLayout()
        self._folder_row.addWidget(self.files_folder, 1)
        self._folder_row.addWidget(browse)
        self.trust.setStyleSheet("margin-left: 22px;")  # visually a sub-option of control
        self.control.toggled.connect(self.trust.setEnabled)
        self.on_top = QCheckBox(tr("set.on_top"))
        self.spellcheck = QCheckBox(tr("set.spellcheck"))
        self.titles = QCheckBox(tr("set.titles"))
        for cb in (self.react, self.greet, self.widgets, self.web, self.control, self.trust, self.files,
                   self.trust_files):
            form.addRow("", cb)
        form.addRow(tr("set.files_folder"), self._folder_row)
        fh = QLabel(tr("set.files_hint"))
        fh.setObjectName("Hint")
        fh.setWordWrap(True)
        form.addRow("", fh)
        for cb in (self.on_top, self.spellcheck, self.titles):
            form.addRow("", cb)

        self.couch = QCheckBox(tr("set.couch"))
        form.addRow("", self.couch)
        self.couch.toggled.connect(self._dirty)
        self.music_file = QLineEdit()
        mbrowse = QPushButton(tr("set.browse"))
        mbrowse.clicked.connect(self._browse_music)
        mrow = QHBoxLayout()
        mrow.addWidget(self.music_file, 1)
        mrow.addWidget(mbrowse)
        self.music_bpm = QSpinBox()
        self.music_bpm.setRange(40, 240)
        self.music_bpm.setFixedWidth(90)
        form.addRow(tr("set.music"), mrow)
        form.addRow(tr("set.music_bpm"), self.music_bpm)
        self.music_out = QComboBox()
        form.addRow(tr("set.music_out"), self.music_out)
        self.music_out.activated.connect(self._dirty)
        self.music_file.textEdited.connect(self._dirty)
        self.music_bpm.valueChanged.connect(self._dirty)
        self.hotkey = QLineEdit()
        self.hide_hotkey = QLineEdit()
        self.models = QLineEdit()
        self.brain = QComboBox()
        for key, models in BRAINS.items():
            self.brain.addItem(tr(f"set.brain_{key}"), models)
        self.brain.addItem(tr("set.brain_custom"), None)
        self.brain.activated.connect(self._brain_chosen)
        form.addRow(tr("set.hotkey"), self.hotkey)
        form.addRow(tr("set.hide_hotkey"), self.hide_hotkey)
        form.addRow(tr("set.brain"), self.brain)
        form.addRow(tr("set.models"), self.models)
        bh = QLabel(tr("set.brain_hint"))
        bh.setObjectName("Hint")
        bh.setWordWrap(True)
        form.addRow("", bh)
        reset = QPushButton(tr("set.reset_general"))
        reset.clicked.connect(self._reset_general)
        hint = QLabel(tr("set.reset_hint"))
        hint.setObjectName("Hint")
        form.addRow("", reset)
        form.addRow("", hint)

        for w in (self.lang, self.bubbles, self.vision):
            w.currentIndexChanged.connect(self._dirty)
        for w in (self.banter, self.react, self.greet, self.widgets, self.web, self.control, self.trust, self.files,
                  self.trust_files, self.on_top, self.spellcheck, self.titles):
            w.toggled.connect(self._dirty)
        for w in (self.b_lo, self.b_hi):
            w.valueChanged.connect(self._dirty)
        for w in (self.hotkey, self.hide_hotkey, self.models, self.files_folder):
            w.textEdited.connect(self._dirty)
        return page

    def _characters_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Page")
        self.list = QListWidget()
        self.list.setFixedWidth(170)
        self.list.currentRowChanged.connect(self._select_row)

        form_w = QWidget()
        form_w.setObjectName("Form")
        form = QFormLayout(form_w)
        form.setContentsMargins(12, 4, 12, 12)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.c_name = QLineEdit()
        self.c_aliases = QLineEdit()
        self.c_aliases.setPlaceholderText(tr("ch.aliases_hint"))
        self.c_color = QPushButton()
        self.c_color.clicked.connect(self._pick_color)
        self.c_size = _SliderBox(0.3, 3.0, 0.05, "")
        self.c_walk = _SliderBox(0.2, 3.0, 0.05, "")
        self.c_energy = _SliderBox(0.0, 2.0, 0.05, "")
        self.c_climb = _SliderBox(0.0, 1.0, 0.01, " %", percent=True)
        self.c_restless = _SliderBox(0.0, 5.0, 0.05, "")
        self.c_apikey = QLineEdit()
        self.c_apikey.setEchoMode(QLineEdit.EchoMode.Password)
        self.c_apikey.setPlaceholderText(tr("ch.key_placeholder"))
        self.c_apikey.setClearButtonEnabled(True)
        eye = self.c_apikey.addAction(_glyph("👁"), QLineEdit.ActionPosition.TrailingPosition)
        eye.setToolTip(tr("ch.key_show"))
        eye.triggered.connect(lambda: self.c_apikey.setEchoMode(
            QLineEdit.EchoMode.Normal if self.c_apikey.echoMode() == QLineEdit.EchoMode.Password
            else QLineEdit.EchoMode.Password))
        self.c_apikey.textEdited.connect(self._key_edited)
        self._char_keys: dict[str, str] = {}
        self._saved_keys: dict[str, str] = {}
        self.c_personality = QPlainTextEdit()
        self.c_personality.setObjectName("Long")
        self.c_personality.setMinimumHeight(150)
        self.c_voice = QPlainTextEdit()
        self.c_voice.setObjectName("Long")
        self.c_voice.setMinimumHeight(70)
        self.c_memory = QLabel()
        self.c_memory.setObjectName("Memory")
        self.c_memory.setWordWrap(True)
        self.c_memory.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.c_presence = QPushButton()
        self.c_presence.clicked.connect(self._toggle_presence)
        energy_hint = QLabel(tr("ch.energy_hint"))
        energy_hint.setObjectName("Hint")

        form.addRow(tr("ch.name"), self.c_name)
        form.addRow(tr("ch.aliases"), self.c_aliases)
        form.addRow(tr("ch.color"), self.c_color)
        form.addRow(tr("ch.size"), self.c_size)
        form.addRow(tr("ch.walk"), self.c_walk)
        form.addRow(tr("ch.energy"), self.c_energy)
        form.addRow("", energy_hint)
        form.addRow(tr("ch.climb"), self.c_climb)
        form.addRow(tr("ch.restless"), self.c_restless)
        form.addRow(tr("ch.key"), self.c_apikey)
        key_hint = QLabel(tr("ch.key_hint"))
        key_hint.setObjectName("Hint")
        key_hint.setWordWrap(True)
        form.addRow("", key_hint)
        sec = QLabel(tr("ch.personality"))
        sec.setObjectName("Section")
        form.addRow(sec)
        form.addRow(self.c_personality)
        sec = QLabel(tr("ch.voice"))
        sec.setObjectName("Section")
        form.addRow(sec)
        form.addRow(self.c_voice)
        sec = QLabel(tr("ch.remembers"))
        sec.setObjectName("Section")
        form.addRow(sec)
        form.addRow(self.c_memory)
        form.addRow(self.c_presence)
        self.c_reset = QPushButton(tr("ch.reset"))
        self.c_reset.clicked.connect(self._reset_character)
        reset_all = QPushButton(tr("ch.reset_all"))
        reset_all.clicked.connect(self._reset_all_characters)
        resets = QHBoxLayout()
        resets.addWidget(self.c_reset)
        resets.addWidget(reset_all)
        form.addRow(resets)
        hint = QLabel(tr("set.reset_hint"))
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        form.addRow(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form_w)
        lay = QHBoxLayout(page)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.addWidget(self.list)
        lay.addWidget(scroll, 1)

        for w in (self.c_name, self.c_aliases):
            w.textEdited.connect(self._char_edited)
        for w in (self.c_size, self.c_walk, self.c_energy, self.c_climb, self.c_restless):
            w.box.valueChanged.connect(self._char_edited)
        for w in (self.c_personality, self.c_voice):
            w.textChanged.connect(self._char_edited)
        return page

    def _memory_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Page")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 16)
        sec = QLabel(tr("mem.shared"))
        sec.setObjectName("Section")
        self.m_shared = QLabel()
        self.m_shared.setObjectName("Memory")
        self.m_shared.setWordWrap(True)
        self.m_shared.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.m_shared.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.m_shared)
        self.m_stats = QLabel()
        self.m_stats.setObjectName("Hint")
        open_dir = QPushButton(tr("mem.open"))
        open_dir.clicked.connect(lambda: os.startfile(self.app.memory.root))
        forget = QPushButton(tr("mem.forget"))
        forget.setObjectName("Danger")
        forget.clicked.connect(self._forget)
        row = QHBoxLayout()
        row.addWidget(self.m_stats, 1)
        row.addWidget(open_dir)
        row.addWidget(forget)
        lay.addWidget(sec)
        lay.addWidget(scroll, 1)
        lay.addLayout(row)
        return page

    # ============================================================== loading
    def load_from_app(self) -> None:
        self._saved_keys = character_keys(self.app.director.client.secrets_path)
        self._char_keys = dict(self._saved_keys)
        if self.current:
            self.c_apikey.setText(self._char_keys.get(self.current, ""))
        self._show_roster(self.app.roster)
        self._set_status("")

    def _show_roster(self, roster: Roster) -> None:
        """Fill every control from `roster` (becomes the draft)."""
        self._loading = True
        self.draft = roster
        s = self.draft.settings
        self.lang.setCurrentIndex(max(0, self.lang.findData(s.language)))
        self.bubbles.setCurrentIndex(max(0, self.bubbles.findData(s.bubbles)))
        self.banter.setChecked(s.banter)
        self.b_lo.setValue(s.banter_every[0] / 60)
        self.b_hi.setValue(s.banter_every[1] / 60)
        self.react.setChecked(s.react_to_throws)
        self.greet.setChecked(s.greet_on_arrival)
        self.widgets.setChecked(s.widgets)
        self.web.setChecked(s.web_surfaces)
        self.control.setChecked(s.control)
        self.trust.setChecked(s.trust_windows)
        self.trust.setEnabled(s.control)
        self.files.setChecked(s.files)
        self.trust_files.setChecked(s.trust_files)
        self.trust_files.setEnabled(s.files)
        self.files_folder.setText(s.files_folder)
        self.music_file.setText(s.music_file)
        self.music_bpm.setValue(s.music_bpm)
        self._fill_outputs(s.music_output)
        self.couch.setChecked(s.couch)
        self.files_folder.setEnabled(s.files)
        self.vision.setCurrentIndex(max(0, self.vision.findData(s.vision)))
        self.on_top.setChecked(s.chat_on_top)
        self.spellcheck.setChecked(s.spellcheck)
        self.titles.setChecked(s.share_window_titles)
        self.hotkey.setText(s.hotkey)
        self.hide_hotkey.setText(s.hide_hotkey)
        self.models.setText(", ".join(s.models))
        idx = next((i for i in range(self.brain.count()) if self.brain.itemData(i) == list(s.models)), self.brain.count() - 1)
        self.brain.setCurrentIndex(idx)
        keep = self.current
        self.list.clear()
        for k, c in self.draft.characters.items():
            it = QListWidgetItem(_dot(c.color), c.display_name)
            it.setData(Qt.ItemDataRole.UserRole, k)
            self.list.addItem(it)
        self._loading = False
        keys = list(self.draft.characters)
        self.current = None
        self.list.setCurrentRow(keys.index(keep) if keep in keys else 0)
        self._refresh_memory()

    # =============================================================== resets
    def _reset_general(self) -> None:
        d = load_defaults()
        if d is not None:
            self._show_roster(Roster(d.settings, dict(self.draft.characters)))
            self._dirty()

    def _reset_character(self) -> None:
        d = load_defaults()
        if d is not None and self.current in d.characters:
            self._show_roster(self.draft.with_character(d.characters[self.current]))
            self._dirty()

    def _reset_all_characters(self) -> None:
        d = load_defaults()
        if d is None:
            return
        roster = self.draft
        for key, spec in d.characters.items():
            roster = roster.with_character(spec)
        self._show_roster(roster)
        self._dirty()

    def _select_row(self, row: int) -> None:
        if row < 0:
            return
        key = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        self.current = key
        c = self.draft.characters[key]
        was, self._loading = self._loading, True
        self.c_name.setText(c.display_name)
        self.c_aliases.setText(", ".join(c.aliases))
        self._show_color(c.color)
        self.c_size.set_value(c.size)
        self.c_walk.set_value(c.walk_speed)
        self.c_energy.set_value(c.energy)
        self.c_climb.set_value(c.climb_chance)
        self.c_restless.set_value(c.restlessness)
        self.c_personality.setPlainText(c.personality)
        self.c_voice.setPlainText(c.voice)
        self.c_apikey.setText(self._char_keys.get(key, ""))
        self.c_apikey.setEchoMode(QLineEdit.EchoMode.Password)
        notes = self.app.memory.facts.get("characters", {}).get(key, [])
        self.c_memory.setText("\n".join(f"• {n}" for n in notes) if notes else tr("ch.remembers_none"))
        self._loading = was
        self._update_presence_button()
        d = load_defaults()
        self.c_reset.setEnabled(d is not None and key in d.characters)  # custom characters have no default

    def _refresh_memory(self) -> None:
        facts = self.app.memory.facts.get("shared", [])
        self.m_shared.setText("\n".join(f"• {f}" for f in facts) if facts else tr("ch.remembers_none"))
        pics = len(list(self.app.memory.images.glob("*")))
        self.m_stats.setText(tr("mem.stats", lines=len(self.app.memory.entries), pics=pics))

    # ============================================================== editing
    def _dirty(self, *_a) -> None:
        if not self._loading:
            self._set_status(tr("set.unsaved"))

    def _char_edited(self, *_a) -> None:
        if self._loading or self.current is None:
            return
        c = self.draft.characters[self.current]
        aliases = tuple(a.strip() for a in self.c_aliases.text().split(",") if a.strip())
        c = replace(c, display_name=self.c_name.text().strip() or c.key.title(), aliases=aliases,
                    size=self.c_size.value(), walk_speed=self.c_walk.value(), energy=self.c_energy.value(),
                    climb_chance=self.c_climb.value(), restlessness=self.c_restless.value(),
                    personality=self.c_personality.toPlainText().strip(), voice=self.c_voice.toPlainText().strip())
        self.draft = self.draft.with_character(c)
        item = self.list.currentItem()
        if item is not None and item.text() != c.display_name:
            item.setText(c.display_name)
        self._dirty()

    def _key_edited(self, text: str) -> None:
        if self.current is not None:
            self._char_keys[self.current] = text.strip()
            self._dirty()

    def _show_color(self, color: str) -> None:
        self.c_color.setText(f"  {color.upper()}")
        self.c_color.setIcon(_dot(color, 18))
        self.c_color.setProperty("hex", color)

    def _pick_color(self) -> None:
        if self.current is None:
            return
        old = self.draft.characters[self.current].color
        col = QColorDialog.getColor(QColor(old), self, tr("ch.pick_color"))
        if not col.isValid():
            return
        hex_ = col.name().upper()
        self.draft = self.draft.with_character(replace(self.draft.characters[self.current], color=hex_))
        self._show_color(hex_)
        item = self.list.currentItem()
        if item is not None:
            item.setIcon(_dot(hex_))
        self._dirty()

    def _update_presence_button(self) -> None:
        if self.current is None:
            return
        here = self.current in self.app.present_keys()
        self.c_presence.setText(tr("ch.dismiss") if here else tr("ch.summon"))

    def _toggle_presence(self) -> None:
        if self.current is None:
            return
        if self.current in self.app.present_keys():
            self.app.dismiss(self.current)
        else:
            self.app.summon(self.current)
        self._update_presence_button()

    def _forget(self) -> None:
        if confirm(self, tr("mem.forget").rstrip(". "), tr("mem.forget_q"), tr("mem.forget_yes"), tr("dlg.cancel"),
                   kind="danger", accept_default=False):
            self.app.forget_everything()
            self._refresh_memory()
            if self.current:
                self._select_row(self.list.currentRow())

    # ================================================================= save
    def save(self) -> None:
        hotkey = self.hotkey.text().strip() or "alt+`"
        hide_hotkey = self.hide_hotkey.text().strip() or "alt+shift+`"
        try:
            keys = [parse_hotkey(hotkey), parse_hotkey(hide_hotkey)]
        except ValueError as e:
            self._set_status(tr("set.bad_hotkey", e=e), error=True)
            return
        if keys[0] == keys[1]:
            self._set_status(tr("set.bad_hotkey", e=hide_hotkey + " = " + hotkey), error=True)
            return
        lo, hi = sorted((self.b_lo.value() * 60, self.b_hi.value() * 60))
        models = tuple(m.strip() for m in self.models.text().split(",") if m.strip()) or self.draft.settings.models
        old_lang = self.app.roster.settings.language
        self.draft = self.draft.with_settings(
            language=self.lang.currentData(), bubbles=self.bubbles.currentData(), banter=self.banter.isChecked(),
            banter_every=(max(30.0, lo), max(30.0, hi)), react_to_throws=self.react.isChecked(),
            greet_on_arrival=self.greet.isChecked(),
            chat_on_top=self.on_top.isChecked(), spellcheck=self.spellcheck.isChecked(), widgets=self.widgets.isChecked(), web_surfaces=self.web.isChecked(),
            vision=self.vision.currentData(), control=self.control.isChecked(), trust_windows=self.trust.isChecked(),
            files=self.files.isChecked(), trust_files=self.trust_files.isChecked(),
            files_folder=self.files_folder.text().strip(),
            music_file=self.music_file.text().strip(), music_bpm=self.music_bpm.value(), music_output=self.music_out.currentData() or "", couch=self.couch.isChecked(), share_window_titles=self.titles.isChecked(),
            hotkey=hotkey, hide_hotkey=hide_hotkey, models=models)
        try:
            save_roster(self.app.characters_path, self.draft)
            keys = {k: v for k, v in self._char_keys.items() if v and k in self.draft.characters}
            if keys != self._saved_keys:  # only touch secrets.toml when a key changed
                save_character_keys(self.app.director.client.secrets_path, keys)
                self._saved_keys = dict(keys)
        except OSError as e:
            self._set_status(str(e), error=True)
            return
        self.app.apply_roster(self.draft)
        if self.draft.settings.language != old_lang:
            # rebuild the window in the new language
            self.app.reopen_settings(self.tabs.currentIndex())
            return
        self._set_status(tr("st.saved"))

    def _brain_chosen(self, index: int) -> None:
        models = self.brain.itemData(index)
        if models:
            self.models.setText(", ".join(models))
            self._dirty()

    def _fill_outputs(self, current: str) -> None:
        """Audio outputs Windows has right now, plus the two automatic choices."""
        from ..activities.music import AUTO_SPEAKERS, output_names
        self.music_out.clear()
        self.music_out.addItem(tr("set.music_out_default"), "")
        self.music_out.addItem(tr("set.music_out_auto"), AUTO_SPEAKERS)
        names = output_names()
        if current and current != AUTO_SPEAKERS and current not in names:
            names.append(current)  # unplugged right now: keep the choice
        for n in names:
            self.music_out.addItem("🔊 " + n, n)
        self.music_out.setCurrentIndex(max(0, self.music_out.findData(current)))

    def _browse_music(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("set.music"), self.music_file.text() or str(Path.home() / "Music"),
                                              "Audio (*.mp3 *.wav *.ogg *.flac *.m4a)")
        if path:
            self.music_file.setText(path)
            self._dirty()

    def _browse_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, tr("set.files_folder"),
                                                self.files_folder.text() or default_folder())
        if path:
            self.files_folder.setText(path)
            self._dirty()

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status.setText(text)
        self.status.setStyleSheet("color: #ff8a80;" if error else "")

    def showEvent(self, e) -> None:
        theme.dark_title_bar(self)
        super().showEvent(e)
        self._refresh_memory()
        self._update_presence_button()
        # centre over the chat window, but keep the whole dialog on screen
        owner = self.parentWidget()
        if owner is not None and not getattr(self, "_placed", False):
            self._placed = True
            geo = owner.screen().availableGeometry()
            c = owner.frameGeometry().center()
            x = min(max(c.x() - self.width() // 2, geo.left() + 8), geo.right() - self.width() - 8)
            y = min(max(c.y() - self.height() // 2, geo.top() + 8), geo.bottom() - self.height() - 8)
            self.move(x, y)
