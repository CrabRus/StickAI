"""Spell-checking for the chat input, using the Windows Spell Checking API.

It's the same checker Edge, Word and the Windows text boxes use, so it needs
no extra package and knows every language you have installed in Windows. Each
word is checked by the checkers for its script: Latin words by English and
Slovak, Cyrillic words by Russian and Ukrainian. A word counts as misspelled
only if *every* one of them rejects it, so mixing languages in one message works.

* red wavy underline under misspelled words,
* right-click one: suggestions, "Add to dictionary" (Windows' own user
  dictionary, shared with other apps) and "Ignore",
* character names and aliases are never flagged.
"""
from __future__ import annotations

import ctypes
import logging
import re
from ctypes import POINTER, c_int, c_ulong, c_void_p, c_wchar_p

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QSyntaxHighlighter, QTextCharFormat, QTextCursor

log = logging.getLogger("stickai.spell")

WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
LANGS = {"latin": ("en-US", "sk-SK", "en-GB"), "cyrillic": ("ru-RU", "uk-UA")}
MAX_SUGGESTIONS = 6

try:
    import comtypes
    import comtypes.client
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class IEnumString(IUnknown):
        _iid_ = GUID("{00000101-0000-0000-C000-000000000046}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Next", (["in"], c_ulong, "celt"), (["out"], POINTER(c_void_p), "rgelt"),
                      (["out"], POINTER(c_ulong), "fetched")),
        ]

    class ISpellingError(IUnknown):
        _iid_ = GUID("{B7C82D61-FBE8-4B47-9B27-6C0D2E0DE0A3}")
        _methods_ = [
            COMMETHOD([], HRESULT, "get_StartIndex", (["out"], POINTER(c_ulong), "v")),
            COMMETHOD([], HRESULT, "get_Length", (["out"], POINTER(c_ulong), "v")),
            COMMETHOD([], HRESULT, "get_CorrectiveAction", (["out"], POINTER(c_int), "v")),
        ]

    class IEnumSpellingError(IUnknown):
        _iid_ = GUID("{803E3BD4-2828-4410-8290-418D1D73C762}")
        _methods_ = [COMMETHOD([], HRESULT, "Next", (["out"], POINTER(POINTER(ISpellingError)), "v"))]

    class ISpellChecker(IUnknown):
        _iid_ = GUID("{B6FD0B71-E2BC-4653-8D05-F197E412770B}")
        _methods_ = [
            COMMETHOD([], HRESULT, "get_LanguageTag", (["out"], POINTER(c_void_p), "v")),
            COMMETHOD([], HRESULT, "Check", (["in"], c_wchar_p, "text"),
                      (["out"], POINTER(POINTER(IEnumSpellingError)), "v")),
            COMMETHOD([], HRESULT, "Suggest", (["in"], c_wchar_p, "word"), (["out"], POINTER(POINTER(IEnumString)), "v")),
            COMMETHOD([], HRESULT, "Add", (["in"], c_wchar_p, "word")),
            COMMETHOD([], HRESULT, "Ignore", (["in"], c_wchar_p, "word")),
        ]

    class ISpellCheckerFactory(IUnknown):
        _iid_ = GUID("{8E018A9D-2415-4677-BF08-794EA61F94BB}")
        _methods_ = [
            COMMETHOD([], HRESULT, "get_SupportedLanguages", (["out"], POINTER(POINTER(IEnumString)), "v")),
            COMMETHOD([], HRESULT, "IsSupported", (["in"], c_wchar_p, "tag"), (["out"], POINTER(c_int), "v")),
            COMMETHOD([], HRESULT, "CreateSpellChecker", (["in"], c_wchar_p, "tag"),
                      (["out"], POINTER(POINTER(ISpellChecker)), "v")),
        ]

    _FACTORY_CLSID = GUID("{7AB36653-1796-484B-BDFA-E74F1DB7C1DC}")
    _OK = True
except Exception:  # noqa: BLE001 - not Windows 8+, or comtypes missing
    _OK = False

_free = None
if _OK:
    _free = ctypes.windll.ole32.CoTaskMemFree
    _free.argtypes = [c_void_p]  # 64-bit pointer (the default int argument would overflow)
    _free.restype = None


def _take_str(ptr) -> str:
    value = ctypes.wstring_at(ptr) or ""
    _free(ptr)
    return value


class SpellChecker:
    """Windows checkers for the installed languages; per-word results are cached."""

    def __init__(self):
        self.checkers: dict[str, list] = {"latin": [], "cyrillic": []}
        self.ignored: set[str] = set()
        self._cache: dict[str, bool] = {}
        if not _OK:
            return
        try:
            comtypes.CoInitialize()
        except OSError:
            pass  # already initialised (Qt uses OLE on the main thread)
        try:
            factory = comtypes.client.CreateObject(_FACTORY_CLSID, interface=ISpellCheckerFactory)
            for script, tags in LANGS.items():
                for tag in tags:
                    if factory.IsSupported(tag):
                        self.checkers[script].append(factory.CreateSpellChecker(tag))
            log.info("Spell-check languages: %s", ", ".join(
                t for tags in LANGS.values() for t in tags if factory.IsSupported(t)) or "none installed")
        except Exception as e:  # noqa: BLE001
            log.info("Windows spell checker unavailable: %s", e)

    @property
    def available(self) -> bool:
        return any(self.checkers.values())

    def _for(self, word: str) -> list:
        return self.checkers["cyrillic" if CYRILLIC.search(word) else "latin"]

    def is_ok(self, word: str) -> bool:
        if len(word) < 2 or word.lower() in self.ignored or word.isupper():
            return True  # single letters, names, ABBREVIATIONS
        ok = self._cache.get(word)
        if ok is None:
            checkers = self._for(word)
            ok = not checkers or any(self._clean(c, word) for c in checkers)
            self._cache[word] = ok
        return ok

    @staticmethod
    def _clean(checker, word: str) -> bool:
        try:
            return not bool(checker.Check(word).Next())
        except Exception:  # noqa: BLE001 - Next() returns S_FALSE (no error) as an empty pointer
            return True

    def suggest(self, word: str) -> list[str]:
        out: list[str] = []
        for checker in self._for(word):
            try:
                enum = checker.Suggest(word)
                while len(out) < MAX_SUGGESTIONS:
                    ptr, fetched = enum.Next(1)
                    if not fetched:
                        break
                    s = _take_str(ptr)
                    if s not in out:
                        out.append(s)
            except Exception:  # noqa: BLE001
                continue
        return out

    def add(self, word: str) -> None:
        for checker in self._for(word)[:1]:  # into the main language's user dictionary
            try:
                checker.Add(word)
            except Exception:  # noqa: BLE001
                pass
        self._cache[word] = True

    def ignore(self, word: str) -> None:
        self.ignored.add(word.lower())
        self._cache.pop(word, None)


class SpellHighlighter(QSyntaxHighlighter):
    """Red wavy underline under words the SpellChecker rejects."""

    def __init__(self, document, checker: SpellChecker):
        super().__init__(document)
        self.checker = checker
        self.enabled = True
        self.fmt = QTextCharFormat()
        self.fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
        self.fmt.setUnderlineColor(QColor("#ff6b6b"))

    def set_enabled(self, on: bool) -> None:
        if on != self.enabled:
            self.enabled = on
            self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        if not self.enabled or not self.checker.available:
            return
        for m in WORD.finditer(text):
            if not self.checker.is_ok(m.group()):
                self.setFormat(m.start(), len(m.group()), self.fmt)


def word_at(edit, pos) -> tuple[QTextCursor, str]:
    """The word under a viewport point, selected in a cursor."""
    cur = edit.cursorForPosition(pos)
    block, col = cur.block().text(), cur.positionInBlock()
    for m in WORD.finditer(block):
        if m.start() <= col <= m.end():
            cur.setPosition(cur.block().position() + m.start())
            cur.setPosition(cur.block().position() + m.end(), QTextCursor.MoveMode.KeepAnchor)
            return cur, m.group()
    return cur, ""


def add_actions(menu, edit, highlighter: SpellHighlighter, pos, labels: dict[str, str]) -> None:
    """Put suggestions / Add / Ignore at the top of the input's context menu."""
    if not highlighter.enabled or not highlighter.checker.available:
        return
    cur, word = word_at(edit, pos)
    if not word or highlighter.checker.is_ok(word):
        return
    first = menu.actions()[0] if menu.actions() else None
    items = []
    suggestions = highlighter.checker.suggest(word)
    for s in suggestions:
        a = QAction(s, menu)
        f = a.font()
        f.setBold(True)
        a.setFont(f)
        a.triggered.connect(lambda _=False, s=s: (cur.insertText(s), highlighter.rehighlight()))
        items.append(a)
    if not suggestions:
        none = QAction(labels["none"], menu)
        none.setEnabled(False)
        items.append(none)
    add = QAction(labels["add"], menu)
    add.triggered.connect(lambda: (highlighter.checker.add(word), highlighter.rehighlight()))
    ign = QAction(labels["ignore"], menu)
    ign.triggered.connect(lambda: (highlighter.checker.ignore(word), highlighter.rehighlight()))
    items += [add, ign]
    for a in items:
        menu.insertAction(first, a)
    if first is not None:
        menu.insertSeparator(first)
