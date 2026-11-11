"""Couch time: when you watch something, everybody on the desktop gathers on a
sofa (seen from behind) at the bottom of the screen and watches with you,
only their heads and shoulders peeking over its back.

"Watching" = the active window is fullscreen, or its title looks like a video
site / player, for a few seconds in a row. Purely local: nothing is sent.
"""
from __future__ import annotations

import re
import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen

from ..desktop.scene import Platform
from ..desktop.win32 import Rect
from ..i18n import tr

COUCH = -7  # pseudo window handle of the seat platform (static, like the floor)
VIDEO_WORDS = re.compile(
    r"youtube|netflix|twitch|prime video|disney\+|hbo|max\b|crunchyroll|vimeo|plex|jellyfin|kinopoisk|"
    r"кинопоиск|ivi\b|okko|vlc|mpv|potplayer|media player|movies & tv|кино|фильм|сериал|film|movie|episode|"
    r"серия|stream", re.IGNORECASE)
START_AFTER, STOP_AFTER = 4.0, 6.0


class Couch:
    def __init__(self, app):
        self.app = app
        self.active = False
        self.rect: Rect | None = None  # sofa body (physical px)
        self.seat: Platform | None = None
        self.alpha = 0.0               # fade in / out
        self._since = 0.0
        self._watch_hint = ""
        self.dismissed = False  # you asked to remove it: don't bring it back until you stop watching
        self.manual = False  # started from the chat ("let's watch"): stays until 'stop'

    # ---------------------------------------------------------- detection
    def _watching(self) -> bool:
        sc = self.app.scene
        fg = sc.foreground
        if fg is None:
            return False
        for m in sc.monitors:  # fullscreen on a monitor?
            b = m.bounds
            if fg.rect.left <= b.left + 2 and fg.rect.top <= b.top + 2 and fg.rect.right >= b.right - 2 \
                    and fg.rect.bottom >= b.bottom - 2:
                return True
        return bool(VIDEO_WORDS.search(fg.title))

    def poll(self) -> None:
        """Called with the scene poll (20 Hz)."""
        if not self.app.roster.settings.couch:
            if self.active:
                self.end()
            return
        now = time.monotonic()
        seen = self._watching()
        if not seen:
            self.dismissed = False  # stopped watching: next time the couch may come back
        watching = (seen and not self.dismissed) or (self.manual and self.active)
        if watching != self.active:
            if self._since == 0.0:
                self._since = now
            elif now - self._since > (START_AFTER if watching else STOP_AFTER):
                self._since = 0.0
                self.start() if watching else self.end()
        else:
            self._since = 0.0

    # ------------------------------------------------------------ show
    def start(self, manual: bool = False) -> None:
        self.manual = manual or self.manual
        present = [s for s in self.app.world.stickmen if not s.leaving]
        if self.active or not present:
            return
        sc = self.app.scene
        fg = sc.foreground
        mon = next((m for m in sc.monitors if fg and m.bounds.contains((fg.rect.left + fg.rect.right) / 2,
                                                                       (fg.rect.top + fg.rect.bottom) / 2)), None) \
            or next((m for m in sc.monitors if m.primary), sc.monitors[0] if sc.monitors else None)
        if mon is None:
            return
        h = max(s.h for s in present)
        width = h * (0.55 * len(present) + 0.75)
        floor = mon.work.bottom
        left = mon.work.right - mon.work.width * 0.06 - width  # bottom-right corner
        # back top sits just below their shoulders; the body hides their dangling legs
        self.rect = Rect(int(left), int(floor - 0.72 * h), int(left + width), int(floor))
        seat_y = int(floor - 0.50 * h)
        self.seat = Platform(seat_y, int(left + 0.35 * h), int(left + width - 0.2 * h), COUCH)
        sc.set_extra([self.seat])
        self.active, self.alpha = True, 0.0
        for i, s in enumerate(present):
            x = self.seat.x0 + (i + 0.6) * (self.seat.x1 - self.seat.x0) / (len(present) + 0.2)
            s.leap_to(self.seat, x)
            s.couch_sit = True  # sit down once landed (see update)
        self.app.director.note_event(tr("couch.start"))

    def remove(self) -> None:
        """You asked them to take the couch away (stays away for this video)."""
        self.dismissed = True
        self.end()

    def end(self) -> None:
        self.manual = False
        if not self.active:
            return
        self.active = False
        for s in self.app.world.stickmen:
            s.couch_sit = False
            if s.support is not None and s.support.hwnd == COUCH:
                s.sit_until = s.time  # get up
        self.app.director.note_event(tr("couch.end"))

    def update(self) -> None:
        if self.rect is None:
            return
        self.alpha = min(1.0, self.alpha + 0.06) if self.active else max(0.0, self.alpha - 0.05)
        if self.active:
            for s in self.app.world.stickmen:
                if getattr(s, "couch_sit", False) and s.support is not None and s.support.hwnd == COUCH \
                        and s.state.name not in ("SITTING", "JUMP_PREP", "HOP"):
                    s.facing = 1
                    s._sit_now(10_000, edge=True)  # sit until the show ends
        elif self.alpha <= 0.0:
            # couch gone: anyone still on it drops to the floor
            self.app.scene.set_extra([])
            self.rect, self.seat = None, None

    # ---------------------------------------------------------- drawing
    def bbox(self) -> Rect | None:
        return None if self.rect is None else Rect(self.rect.left - 10, self.rect.top - 10,
                                                    self.rect.right + 10, self.rect.bottom + 10)

    def paint(self, p: QPainter) -> None:
        """Sofa from behind, drawn OVER the stickmen so only heads peek out."""
        if self.rect is None or self.alpha <= 0:
            return
        r = self.rect
        p.save()
        p.setOpacity(self.alpha)
        W, H = r.width, r.height
        leg = H * 0.08
        body = QRectF(r.left + W * 0.08, r.top, W * 0.92, H - leg)
        arm = QRectF(r.left, r.top + H * 0.30, W * 0.12, H * 0.70 - leg)
        stripes = QColor(196, 190, 170), QColor(172, 166, 146)
        for rect, radius in ((arm, H * 0.10), (body, H * 0.16)):
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            p.save()
            p.setClipPath(path)
            sw = max(6.0, W / 26)
            x, i = rect.left(), 0
            while x < rect.right():
                p.fillRect(QRectF(x, rect.top(), sw, rect.height()), stripes[i % 2])
                x += sw
                i += 1
            shade = QLinearGradient(0, rect.top(), 0, rect.bottom())
            shade.setColorAt(0, QColor(255, 255, 255, 40))
            shade.setColorAt(1, QColor(0, 0, 0, 50))
            p.fillRect(rect, shade)
            p.restore()
            p.setPen(QPen(QColor(120, 114, 98), max(1.5, H * 0.012)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(170, 130, 80))
        for fx in (0.03, 0.12, 0.88, 0.96):
            p.drawRoundedRect(QRectF(r.left + W * fx, r.bottom - leg, W * 0.025, leg), 2, 2)
        p.restore()
