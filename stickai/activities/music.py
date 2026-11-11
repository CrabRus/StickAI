"""Music show: a stickman sets out a row of wooden music boxes, then plays your
mp3 by hopping and striking a box on every beat; each strike flashes the box
and sends a coloured note floating up.

The boxes are our own drawing (a wooden crate with a speaker grille), drawn
with QPainter, so no textures are needed.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtMultimedia import QAudioDevice, QAudioOutput, QMediaDevices, QMediaPlayer

from ..desktop.win32 import Rect

NOTE_COLORS = ["#FEC503", "#33CCFF", "#D91100", "#66CC00", "#FF8A3D", "#C77DFF"]
NOTE_GLYPHS = ["♪", "♫", "♩", "♬"]


@dataclass
class Box:
    x: float          # centre x
    y: float          # bottom (sits on the surface)
    size: float
    born: float       # time it popped in
    flash: float = 0.0


@dataclass
class Note:
    x: float
    y: float
    color: str
    glyph: str
    born: float
    drift: float = field(default_factory=lambda: random.uniform(-0.4, 0.4))


# How a device's name says what it is (English / Russian / Slovak Windows names).
HEADPHONE_WORDS = ("headphone", "headset", "earphone", "earbud", "airpods", "buds", "hands-free", "handsfree",
                   "наушник", "гарнитур", "slúchadl", "sluchadl")
SPEAKER_WORDS = ("speaker", "динамик", "колонк", "reproduktor", "repro")

AUTO_SPEAKERS = "auto-speakers"   # music_output value: speakers whenever headphones are the default


def _is_headphones(dev: QAudioDevice) -> bool:
    name = dev.description().lower()
    return any(w in name for w in HEADPHONE_WORDS)


def output_names() -> list[str]:
    """Names of the audio outputs Windows has right now (for the settings list)."""
    return [d.description() for d in QMediaDevices.audioOutputs()]


def pick_output(mode: str) -> QAudioDevice:
    """music_output -> the device to play on.
      ""             Windows' default output
      "auto-speakers" the default, unless it's headphones: then the first speakers found
      anything else  that device by name (falls back to the default if it's gone)"""
    default = QMediaDevices.defaultAudioOutput()
    outs = QMediaDevices.audioOutputs()
    if mode == AUTO_SPEAKERS:
        if not _is_headphones(default):
            return default
        others = [d for d in outs if not _is_headphones(d) and d.id() != default.id()]
        named = [d for d in others if any(w in d.description().lower() for w in SPEAKER_WORDS)]
        return (named or others or [default])[0]
    if mode:
        for d in outs:
            if d.description() == mode:
                return d
    return default


class MusicShow:
    BOXES = 4

    def __init__(self, app):
        self.app = app
        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.player.setAudioOutput(self.audio)
        self._devices = QMediaDevices()
        self._devices.audioOutputsChanged.connect(self._route)  # headphones plugged in / out mid-show
        self.player.mediaStatusChanged.connect(self._status)
        self.performer = None
        self.boxes: list[Box] = []
        self.notes: list[Note] = []
        self.state = "off"         # off | placing | playing | packing
        self.beat = 0.5
        self.next_beat = 0.0
        self.box_i = 0
        self.t0 = 0.0
        self.error = ""

    # ------------------------------------------------------------ control
    @property
    def running(self) -> bool:
        return self.state != "off"

    def start(self, s, path: str, bpm: int) -> str:
        """Begin the show with stickman `s`. Returns "" or an error key."""
        if self.running:
            return "music.busy"
        p = Path(path).expanduser() if path else None
        if p is None or not p.is_file():
            return "music.no_file"
        if not s.grounded or s.support is None:
            return "music.not_ground"
        self.performer = s
        self.beat = 60.0 / max(40, min(240, bpm))
        h, f = s.h, s.facing
        size = 0.4 * h
        gap = size * 1.25
        sup = s.support
        # a row in front of him, kept on the surface he stands on
        xs = [s.pos.x + f * (0.55 * h + i * gap) for i in range(self.BOXES)]
        lo, hi = sup.x0 + size / 2 + 2, sup.x1 - size / 2 - 2
        if xs and (min(xs) < lo or max(xs) > hi):  # no room that way: put them behind him
            xs = [s.pos.x - f * (0.55 * h + i * gap) for i in range(self.BOXES)]
        xs = [min(max(x, lo), hi) for x in xs]
        now = time.monotonic()
        self.boxes = [Box(x, sup.y, size, now + 0.18 * i) for i, x in enumerate(xs)]
        self.notes.clear()
        self.state, self.t0, self.box_i = "placing", now, 0
        s.talk(999)  # stand still for the show
        s.target_x = None
        self._route()
        self.player.setSource(QUrl.fromLocalFile(str(p)))
        return ""

    def _route(self) -> None:
        """Send the song to the output chosen in Settings (e.g. speakers while you wear headphones)."""
        dev = pick_output(getattr(self.app.roster.settings, "music_output", ""))
        if dev.id() != self.audio.device().id():
            self.audio.setDevice(dev)

    def stop(self) -> bool:
        if not self.running:
            return False
        self.player.stop()
        self._pack()
        return True

    def _pack(self) -> None:
        if self.state in ("off", "packing"):
            return
        self.state, self.t0 = "packing", time.monotonic()
        if self.performer is not None:
            self.performer.idle_for = 0.5  # free to walk again

    def _status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._pack()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.error = "music.bad_file"
            self._pack()

    # -------------------------------------------------------------- update
    def update(self) -> None:
        if not self.running:
            return
        now = time.monotonic()
        s = self.performer
        if s is None or s not in self.app.world.stickmen or s.state.name == "DRAGGED":
            self.stop()
        if self.state == "placing" and now > self.boxes[-1].born + 0.4:
            self.state = "playing"
            self.next_beat = now + 0.2
            self.player.play()
        elif self.state == "playing" and now >= self.next_beat:
            self.next_beat += self.beat
            if self.next_beat < now:  # fell behind (e.g. hidden): resync
                self.next_beat = now + self.beat
            self._strike(now)
        elif self.state == "packing" and now > self.t0 + 0.6:
            self.state, self.boxes = "off", []
        self.notes = [n for n in self.notes if now - n.born < 2.2]

    def _strike(self, now: float) -> None:
        s = self.performer
        b = self.boxes[self.box_i % len(self.boxes)]
        self.box_i += 1
        if s is not None and s.grounded:
            s.facing = 1 if b.x >= s.pos.x else -1
            s.gesture, s.gesture_until = "point", s.time + self.beat * 0.8
            s.point_at = (b.x, b.y - b.size * 0.6)
            if self.box_i % 4 == 0:
                s.squash_v -= 3.0  # a little bounce on the downbeat
        b.flash = now
        self.notes.append(Note(b.x, b.y - b.size, random.choice(NOTE_COLORS), random.choice(NOTE_GLYPHS), now))

    # -------------------------------------------------------------- drawing
    def bbox(self) -> Rect | None:
        if not self.boxes and not self.notes:
            return None
        xs = [b.x for b in self.boxes] + [n.x for n in self.notes]
        size = max((b.size for b in self.boxes), default=60)
        top = min([b.y - b.size for b in self.boxes] + [n.y for n in self.notes]) - size * 3
        bottom = max((b.y for b in self.boxes), default=top + size)
        return Rect(int(min(xs) - size * 2), int(top), int(max(xs) + size * 2), int(bottom + 4))

    def paint(self, p: QPainter) -> None:
        now = time.monotonic()
        for b in self.boxes:
            k = min(1.0, max(0.0, (now - b.born) / 0.25))           # pop in
            if self.state == "packing":
                k *= max(0.0, 1.0 - (now - self.t0) / 0.5)           # shrink away
            if k <= 0:
                continue
            k = 1 - (1 - k) ** 3 * (1 if k < 1 else 0)               # ease out
            hit = max(0.0, 1.0 - (now - b.flash) / 0.25) if b.flash else 0.0
            size = b.size * k * (1 + 0.08 * hit)
            r = QRectF(b.x - size / 2, b.y - size, size, size)
            self._box(p, r, hit)
        font = QFont("Segoe UI Symbol")
        for n in self.notes:
            age = now - n.born
            a = max(0.0, 1.0 - age / 2.2)
            base = self.boxes[0].size if self.boxes else 60
            font.setPixelSize(max(16, int(base * (0.75 + 0.25 * min(1.0, age * 3)))))
            p.setFont(font)
            c = QColor(n.color)
            c.setAlphaF(a)
            p.setPen(c)
            x = n.x + math.sin(age * 3) * 14 + n.drift * age * 60
            p.drawText(QPointF(x, n.y - age * 110), n.glyph)

    @staticmethod
    def _box(p: QPainter, r: QRectF, hit: float) -> None:
        wood, dark = QColor(150, 92, 52), QColor(92, 54, 30)
        if hit:
            wood = wood.lighter(100 + int(45 * hit))
        p.setPen(QPen(dark, max(2.0, r.width() * 0.05)))
        p.setBrush(wood)
        p.drawRect(r)
        # speaker grille: a grid of small dark holes
        m = r.width() * 0.16
        inner = r.adjusted(m, m, -m, -m)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(dark)
        n = 5
        cell = inner.width() / n
        for i in range(n):
            for j in range(n):
                if (i + j) % 2 == 0:
                    p.drawRect(QRectF(inner.left() + i * cell, inner.top() + j * cell, cell * 0.8, cell * 0.8))
