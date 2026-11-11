"""Timers and reminders: "Blue, remind me in 20 minutes to stretch".

Gemini only turns the request into (seconds, text) once; the countdown runs
here, so a reminder costs no further requests. Reminders are saved in
memory/reminders.json and survive a restart: one that came due while
StickAI was closed fires right after start, marked as missed.

When one fires, its character drops in if they're not on the desktop, shows
the text in a full speech bubble (whatever the bubble setting is), waves,
and a Windows notification appears (which also shows while everything is
hidden with the hide hotkey).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from ..i18n import tr

log = logging.getLogger("stickai.reminders")
MAX_SECONDS = 7 * 24 * 3600   # a week
MAX_PENDING = 20


@dataclass
class Reminder:
    due: float      # time.time()
    key: str        # character who reminds you
    text: str


class ReminderBook:
    def __init__(self, app, path: Path):
        self.app = app
        self.path = path
        self.items: list[Reminder] = []
        self._load()
        self.timer = QTimer(interval=1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        QTimer.singleShot(4000, self._tick)  # overdue ones from while we were closed

    # ------------------------------------------------------------ storage
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [Reminder(float(r["due"]), str(r["key"]), str(r["text"])) for r in raw]
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError) as e:
            log.warning("reminders.json unreadable, ignoring it: %s", e)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps([asdict(r) for r in self.items], ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        except OSError as e:
            log.warning("can't save reminders: %s", e)

    # ------------------------------------------------------------ editing
    def add(self, key: str, seconds: float, text: str) -> Reminder | None:
        text = " ".join(str(text).split())[:200]
        if not text or not 1 <= seconds <= MAX_SECONDS or len(self.items) >= MAX_PENDING:
            return None
        r = Reminder(time.time() + seconds, key, text)
        self.items.append(r)
        self.items.sort(key=lambda x: x.due)
        self._save()
        return r

    def cancel(self, text: str = "") -> int:
        """Remove reminders whose text contains `text` (all of them if empty)."""
        want = text.strip().lower()
        keep = [r for r in self.items if want and want not in r.text.lower()]
        n = len(self.items) - len(keep)
        self.items = keep
        self._save()
        return n

    def describe(self) -> list[str]:
        """For the prompt: what's pending."""
        name = self.app.director.name
        return [f'- at {datetime.fromtimestamp(r.due):%a %H:%M}: {name(r.key)} reminds the user "{r.text}"'
                for r in self.items]

    # ------------------------------------------------------------- firing
    def _tick(self) -> None:
        now = time.time()
        due = [r for r in self.items if r.due <= now]
        if not due:
            return
        self.items = [r for r in self.items if r.due > now]
        self._save()
        for r in due:
            self._fire(r, missed=now - r.due > 90)

    def _fire(self, r: Reminder, missed: bool) -> None:
        app = self.app
        name = app.director.name(r.key)
        text = tr("rem.missed", text=r.text) if missed else r.text
        log.info("reminder: %s -> %s", name, text)
        app.director.note_event(tr("rem.fired", name=name, text=text))
        if not app.hidden:
            if app.world.get(r.key) is None:
                app.summon(r.key)
                QTimer.singleShot(1800, lambda: self._say(r.key, text))  # after they've landed
            else:
                self._say(r.key, text)
        tray = getattr(app, "tray", None)
        if tray is not None and QSystemTrayIcon.supportsMessages():
            tray.showMessage(f"⏰ {name}", text, QSystemTrayIcon.MessageIcon.Information, 15000)
        else:
            QApplication.beep()

    def _say(self, key: str, text: str) -> None:
        if self.app.world.get(key) is not None:
            self.app.say(key, "⏰ " + text, "wave", "user", force_bubble=True)
