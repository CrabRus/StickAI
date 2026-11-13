"""Card game: when you've been away from the PC for a couple of minutes (no mouse / keyboard,
and you're not watching a video), everybody on the desktop sits down at a little table at the
bottom of the screen and plays cards. The moment you touch the mouse or keyboard they put the
cards away and get up.

Seats: a stool left and right of the table; more players sit behind it (the table hides their
legs). They hold a fan of cards, play one in turn, and whoever takes the round throws his arms
up. Purely local animation: nothing is sent anywhere.
"""
from __future__ import annotations

import math
import random
import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF

from ..desktop import win32
from ..desktop.scene import Platform
from ..desktop.win32 import Rect
from ..i18n import tr

CARDS = -8          # pseudo window handle of the seats platform
BACK_AFTER = 0.8    # s of idle time below which you're "back"
SUITS = ("#d23c3c", "#1d1d1d")
BACKS = ("#3b6fd4", "#c0392b")
RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A")


class Cards:
    def __init__(self, app):
        self.app = app
        self.active = False
        self.manual = False        # started from the chat: stays until "stop" / you come back
        self.alpha = 0.0
        self.table: Rect | None = None     # table top .. floor (physical px)
        self.seat: Platform | None = None
        self.h = 60.0
        self.players: list[tuple[str, float, int]] = []  # (key, seat x, facing)
        self.pile: list[tuple[float, float, str, str]] = []  # cards on the table: (dx, angle, suit colour, rank)
        self.turn = 0
        self.next_play = 0.0
        self.round_len = 6
        self._pending: tuple[float, str] | None = None  # (when the played card lands, who)
        self._clear_at = 0.0

    # ---------------------------------------------------------- detection
    def _may_start(self) -> bool:
        app = self.app
        if app.hidden or app.paused or app.tasks.busy or app.fight.active or app.music.running:
            return False
        if app.couch.active or app.couch._watching():  # watching a video: that's not "away"
            return False
        return any(not s.leaving for s in app.world.stickmen)

    def poll(self) -> None:
        """Called with the scene poll (20 Hz)."""
        st = self.app.roster.settings
        if not st.cards and not self.manual:
            if self.active:
                self.end()
            return
        idle = win32.idle_seconds()
        if self.active:
            if self.app.fight.active or self.app.couch.active:
                self.end()
            elif idle < BACK_AFTER and not self.manual:
                self.end()  # you're back
        elif st.cards and idle >= st.cards_after * 60.0 and self._may_start():
            self.start()

    # ------------------------------------------------------------ show
    def start(self, manual: bool = False) -> bool:
        present = [s for s in self.app.world.stickmen if not s.leaving]
        if self.active or not present:
            return False
        sc = self.app.scene
        mon = next((m for m in sc.monitors if m.primary), sc.monitors[0] if sc.monitors else None)
        if mon is None:
            return False
        self.manual = manual
        h = self.h = max(s.h for s in present)
        floor = mon.work.bottom
        cx = mon.work.left + mon.work.width * 0.32  # left of the middle (the couch is bottom right)
        tw = 1.45 * h
        self.table = Rect(int(cx - tw / 2), int(floor - 0.56 * h), int(cx + tw / 2), int(floor))
        seat_y = int(floor - 0.34 * h)
        self.seat = Platform(seat_y, int(cx - tw / 2 - 0.75 * h), int(cx + tw / 2 + 0.75 * h), CARDS)
        sc.set_extra([self.seat], owner="cards")
        spots = [(cx - tw / 2 - 0.33 * h, 1), (cx + tw / 2 + 0.33 * h, -1),
                 (cx - 0.30 * h, 1), (cx + 0.30 * h, -1), (cx, 1)]
        random.shuffle(present)
        self.players = []
        for s, (x, f) in zip(present, spots):
            if s.state.name == "GRAPPLE":
                s.drop_rod()
                s.start_fall()
            s.leap_to(self.seat, x)
            self.players.append((s.key, x, f))
        self.active, self.alpha = True, 0.0
        self.pile, self.turn, self._pending, self._clear_at = [], 0, None, 0.0
        self.next_play = time.monotonic() + 4.0
        self.round_len = random.randint(2, 3) * max(2, len(self.players))
        self.app.director.note_event(tr("cards.start"))
        return True

    def end(self) -> None:
        self.manual = False
        if not self.active:
            return
        self.active = False
        for key, _x, _f in self.players:
            s = self.app.world.get(key)
            if s is None:
                continue
            s.cards = False
            if s.support is not None and s.support.hwnd == CARDS:
                s.sit_until = s.time  # get up
        self.app.director.note_event(tr("cards.end"))

    def _seated(self):
        for key, _x, _f in self.players:
            s = self.app.world.get(key)
            if s is not None and s.cards:
                yield s

    def update(self) -> None:
        if self.table is None:
            return
        self.alpha = min(1.0, self.alpha + 0.05) if self.active else max(0.0, self.alpha - 0.05)
        if not self.active:
            if self.alpha <= 0.0:
                self.app.scene.set_extra([], owner="cards")  # anyone still there drops to the floor
                self.table, self.seat, self.players, self.pile = None, None, [], []
            return
        for key, _x, f in self.players:
            s = self.app.world.get(key)
            if s is None:
                continue
            on_seat = s.support is not None and s.support.hwnd == CARDS
            if on_seat and s.state.name not in ("SITTING", "JUMP_PREP", "HOP"):
                s.facing = f
                s._sit_now(10_000, edge=True)  # sit until you come back
            s.cards = on_seat and s.state.name == "SITTING"
        self._play()

    def _play(self) -> None:
        now = time.monotonic()
        seated = list(self._seated())
        if self._pending and now >= self._pending[0]:
            self._pending = None
            self.pile.append((random.uniform(-0.28, 0.28), random.uniform(-0.5, 0.5),
                              random.choice(SUITS), random.choice(RANKS)))
            if len(self.pile) >= self.round_len:  # round over: somebody takes the trick
                if seated:
                    win = random.choice(seated)
                    win.cheer_until = win.time + 1.6
                self.next_play = now + 2.6
                self._clear_at = now + 1.4
                return
        if self._clear_at and now >= self._clear_at:
            self._clear_at = 0.0
            self.pile = []
            self.round_len = random.randint(2, 3) * max(2, len(seated))
        if not seated or now < self.next_play or self._pending:
            return
        s = seated[self.turn % len(seated)]
        self.turn += 1
        s.card_play_until = s.time + 0.55
        self._pending = (now + 0.40, s.key)
        self.next_play = now + random.uniform(1.2, 2.6)

    # ---------------------------------------------------------- drawing
    def bbox(self) -> Rect | None:
        if self.table is None or self.seat is None:
            return None
        h = self.h
        return Rect(int(self.seat.x0 - 0.6 * h), int(self.table.bottom - 2.2 * h),
                    int(self.seat.x1 + 0.6 * h), self.table.bottom + 4)

    def paint(self, p: QPainter) -> None:
        """Table, stools and cards, drawn OVER the stickmen (the table hides the legs behind it)."""
        if self.table is None or self.seat is None or self.alpha <= 0:
            return
        h, r = self.h, self.table
        p.save()
        p.setOpacity(self.alpha)
        wood, dark, felt = QColor(150, 103, 60), QColor(105, 70, 40), QColor(46, 125, 80)
        top_y, depth = r.top, 0.13 * h
        # stools under the side seats
        p.setPen(Qt.PenStyle.NoPen)
        sy = self.seat.y
        for x in (r.left - 0.33 * h, r.right + 0.33 * h):
            p.setBrush(dark)
            for lx in (-0.13, 0.105):
                p.drawRect(QRectF(x + lx * h, sy, 0.025 * h, r.bottom - sy))
            p.setBrush(wood)
            p.drawRoundedRect(QRectF(x - 0.17 * h, sy - 0.01 * h, 0.34 * h, 0.05 * h), 3, 3)
        # the table: its top seen a little from above (green felt), the front edge, a skirt, legs
        top = QPolygonF([QPointF(r.left + 0.08 * h, top_y - depth), QPointF(r.right - 0.08 * h, top_y - depth),
                         QPointF(r.right, top_y), QPointF(r.left, top_y)])
        p.setBrush(felt)
        p.setPen(QPen(QColor(30, 90, 55), max(1.0, 0.012 * h)))
        p.drawPolygon(top)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(wood)
        p.drawRect(QRectF(r.left, top_y, r.width, 0.06 * h))
        p.setBrush(QColor(128, 86, 50))
        p.drawRect(QRectF(r.left + 0.05 * h, top_y + 0.06 * h, r.width - 0.10 * h, 0.16 * h))
        p.setBrush(dark)
        for lx in (r.left + 0.07 * h, r.right - 0.11 * h):
            p.drawRect(QRectF(lx, top_y + 0.06 * h, 0.04 * h, r.bottom - top_y - 0.06 * h))
        # cards on the table, lying flat
        cx = (r.left + r.right) / 2
        for dx, ang, suit, rank in self.pile:
            self._flat_card(p, cx + dx * r.width, top_y - depth * 0.5, ang, suit, rank)
        # the fans of cards in their hands
        for s in self._seated():
            if s.time < s.cheer_until:
                continue
            back = BACKS[sum(map(ord, s.key)) % 2]
            a, b = s.joints["hand0"], s.joints["hand1"]
            if s.time < s.card_play_until:  # the one being played, in the near hand
                self._card(p, b.x, b.y + 0.02 * h, 0.0, back)
                self._fan(p, a.x, a.y, s.facing, 3, back)
            else:
                self._fan(p, (a.x + b.x) / 2, (a.y + b.y) / 2, s.facing, 4, back)
        p.restore()

    def _card(self, p: QPainter, x: float, y: float, ang: float, back: str) -> None:
        w, hh, m = 0.085 * self.h, 0.12 * self.h, 0.012 * self.h
        p.save()
        p.translate(x, y)
        p.rotate(math.degrees(ang))
        p.setPen(QPen(QColor(60, 60, 60), max(0.8, 0.008 * self.h)))
        p.setBrush(QColor(back))
        p.drawRoundedRect(QRectF(-w / 2, -hh, w, hh), 2, 2)
        p.setPen(QPen(QColor(255, 255, 255, 170), max(0.6, 0.006 * self.h)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(-w / 2 + m, -hh + m, w - 2 * m, hh - 2 * m), 1, 1)
        p.restore()

    def _fan(self, p: QPainter, x: float, y: float, facing: int, n: int, back: str) -> None:
        """A hand of cards held up, fanned out (we see their backs)."""
        for i in range(n):
            ang = (i - (n - 1) / 2) * 0.22 + facing * 0.15
            self._card(p, x + facing * 0.01 * self.h, y + 0.01 * self.h, ang, back)

    def _flat_card(self, p: QPainter, x: float, y: float, ang: float, suit: str, rank: str) -> None:
        w, d = 0.11 * self.h, 0.075 * self.h
        p.save()
        p.translate(x, y)
        p.scale(1.0, 0.5)  # lying on the table, seen a little from above
        p.rotate(math.degrees(ang))
        path = QPainterPath()
        path.addRoundedRect(QRectF(-w / 2, -d, w, 2 * d), 2, 2)
        p.setPen(QPen(QColor(90, 90, 90), max(0.8, 0.008 * self.h)))
        p.setBrush(QColor(250, 250, 246))
        p.drawPath(path)
        p.setPen(QColor(suit))
        f = p.font()
        f.setPixelSize(max(7, int(0.09 * self.h)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(-w / 2, -d, w, 2 * d), Qt.AlignmentFlag.AlignCenter, rank)
        p.restore()
