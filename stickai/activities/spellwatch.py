"""Spelling watch (opt-in): while you type in another app's text field, a stickman spots a
typo, goes over to it, jabs at it and offers the fix in a bubble. A click on the bubble fixes
it; otherwise nothing changes.

The field you're typing in is read locally (UI Automation) about once a second, and checked by
the Windows spell checker on this PC: nothing is sent anywhere. Password fields are never
read. Only after you pause typing, and only for words you've finished (not the one you're on).
"""
from __future__ import annotations

import logging
import os
import threading
import time

from PySide6.QtCore import QObject, Signal

from ..tools import uia

log = logging.getLogger("stickai.spellwatch")
POLL = 1.1          # s between reads of the focused field
COOLDOWN = 25.0     # s between two offers
FREE_STATES = ("IDLE", "WALKING", "SITTING", "LANDING", "TURNING")


class SpellWatch(QObject):
    seen = Signal(object)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.enabled = False
        self._text = None
        self._stable = 0
        self._next = 0.0
        self.offered: set[str] = set()  # words already offered (not again this session)
        self.seen.connect(self._on_seen)
        threading.Thread(target=self._loop, daemon=True, name="spellwatch").start()

    def update_enabled(self) -> None:
        st = self.app.roster.settings
        self.enabled = bool(st.control and st.read_fields and st.spell_watch)

    # ------------------------------------------------------------ worker thread
    def _loop(self) -> None:
        while True:
            time.sleep(POLL)
            if not self.enabled or self.app.hidden or self.app.paused:
                continue
            try:
                f = uia.focused_field()
            except Exception:  # noqa: BLE001
                f = None
            self.seen.emit(f)

    # ------------------------------------------------------------ main thread
    def _on_seen(self, f) -> None:
        if not self.enabled or f is None or f["pid"] == os.getpid():
            self._text, self._stable = None, 0
            return
        text = f["text"]
        if text != self._text:  # still typing
            self._text, self._stable = text, 0
            return
        self._stable += 1
        if self._stable != 1 or time.monotonic() < self._next:
            return  # look once per pause in typing
        app = self.app
        if app.tasks.busy or app.fight.active or app.couch.active or getattr(app, "cards", None) and app.cards.active:
            return
        typo = self._first_typo(text)
        if typo is None:
            return
        bad, good = typo
        key = self._who(f["rect"])
        if key and app.tasks.offer_typo(key, f, bad, good):
            self.offered.add(bad)
            self._next = time.monotonic() + COOLDOWN
            log.info("spelling watch: offered a fix")

    def _first_typo(self, text: str) -> tuple[str, str] | None:
        from ..ui.spellcheck import WORD
        checker = self.app.chat.input.spell.checker
        if not checker.available:
            return None
        for m in WORD.finditer(text):
            if m.end() == len(text):  # the word you're on may not be finished
                break
            w = m.group()
            if len(w) < 3 or w in self.offered or checker.is_ok(w):
                continue
            sugg = checker.suggest(w)
            if sugg:
                return w, sugg[0]
        return None

    def _who(self, rect) -> str | None:
        """The free stickman nearest to the field."""
        cx = (rect[0] + rect[2]) / 2
        free = [s for s in self.app.world.stickmen if not s.leaving and s.state.name in FREE_STATES
                and not getattr(s, "fighting", False) and not getattr(s, "cards", False)
                and not getattr(s, "couch_sit", False)]
        if not free:
            return None
        return min(free, key=lambda s: abs(s.pos.x - cx)).key
