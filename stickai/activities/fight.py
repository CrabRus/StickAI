"""Fight club: on command the stickmen gather on the floor and fight.

Each fighter picks the nearest opponent and:
  * runs at him (a charge), or leaps at him with a flying kick from a few steps away;
  * up close: punches (jab / cross, sometimes a combo) and kicks;
  * the one attacked may block (forearms up): a blocked hit only pushes him back;
  * a clean punch staggers him, a kick or a flying kick sends him flying;
  * after taking a few hits he may run away for a moment, then comes back.
Purely local animation: no AI request per hit. It ends after a while or on "stop".
Who is aggressive / cautious comes from the character sheet (energy, restlessness).
"""
from __future__ import annotations

import math
import random
import time

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen

from ..anim.mathutil import Vec, clamp
from ..anim.stickman import State
from ..desktop.win32 import Rect
from ..i18n import tr

DURATION = 28.0          # seconds of fighting
PUNCH_TIME, KICK_TIME, BLOCK_TIME = 0.34, 0.55, 0.55
REACH_PUNCH, REACH_KICK = 0.82, 1.02        # in body heights, from body to body
CLOSE = 0.62                                # ideal distance to fight at (h): fists reach
SPARK_TIME = 0.28
BUSY_AIR = (State.FALLING, State.HOP, State.JUMP_PREP, State.GRAPPLE)
ROD_MIN, ROD_MAX = 1.8, 5.5                 # hooking the opponent from this far (h)


class Fighter:
    def __init__(self, s):
        self.s = s
        e = clamp(getattr(s, "energy", 1.0), 0.2, 2.0)
        self.aggression = 0.55 + 0.40 * e                       # attacks more often
        self.block_p = clamp(0.58 - 0.17 * e, 0.22, 0.55)       # calm ones block more
        self.flee_p = clamp(0.45 - 0.20 * e, 0.08, 0.40)        # lazy ones run away sooner
        self.flying_p = clamp(0.10 + 0.12 * e, 0.10, 0.35)      # energetic ones leap more
        self.next_attack = time.monotonic() + random.uniform(0.3, 1.0)
        self.landed = False      # this move already connected (one hit per move)
        self.hits = 0            # taken
        self.dealt = 0
        self.streak = 0          # hits taken since the last time he ran
        self.flee_until = 0.0
        self.combo = 0
        self.regather = 0.0      # next time he may jump over to the opponent's surface
        self.rod_p = clamp(0.20 + 0.10 * e, 0.2, 0.4)         # chance to hook from a distance
        self.rod: dict | None = None  # the fishing rod in use: {t0, fly, target, pull_until}
        self.rod_ready = time.monotonic() + random.uniform(2.0, 5.0)  # the rod isn't used every second


class Fight:
    def __init__(self, app):
        self.app = app
        self.active = False
        self.fighters: dict[str, Fighter] = {}
        self.until = 0.0
        self.sparks: list[tuple[float, float, float, float, bool]] = []  # x, y, t0, h, blocked
        self.arena = None

    # ------------------------------------------------------------ control
    def start(self, keys: list[str] | None = None) -> bool:
        world = self.app.world
        people = [s for s in world.stickmen if not s.leaving and (not keys or s.key in keys)]
        if not people:
            return False
        if self.active:  # more people join / the fight goes on longer
            self.until = time.monotonic() + DURATION
        else:
            self.until = time.monotonic() + DURATION
            self.active = True
            self.app.music.stop()
            self.app.couch.end()
        for s in people:
            if s.key not in self.fighters:
                self.fighters[s.key] = Fighter(s)
                s.fighting = True
                s.gesture = ""
                s.target_x = None
                s.sit_until = s.time  # get up
        self._gather()
        names = ", ".join(f.s.name for f in self.fighters.values())
        self.app.director.note_event(tr("fight.start", names=names))
        return True

    def _gather(self) -> None:
        """Everybody down onto one floor (the one under the middle of the group)."""
        sc = self.app.scene
        fs = [f.s for f in self.fighters.values()]
        cx = sum(s.pos.x for s in fs) / len(fs)
        floor = sc.floor_under(cx, min(s.pos.y for s in fs)) or next((p for p in sc.platforms if p.is_floor), None)
        self.arena = floor
        if floor is None:
            return
        span = floor.x1 - floor.x0
        mid = clamp(cx, floor.x0 + 0.2 * span, floor.x1 - 0.2 * span)
        for i, s in enumerate(fs):
            on_it = s.support is not None and s.support.is_floor and abs(s.support.y - floor.y) < 4
            if on_it or s.state == State.DRAGGED:
                continue
            x = mid + (i - (len(fs) - 1) / 2) * 1.6 * s.h
            s.leap_to(floor, clamp(x, floor.x0 + s.h, floor.x1 - s.h))

    def stop(self, quiet: bool = False) -> bool:
        if not self.active:
            return False
        self.active = False
        fs = list(self.fighters.values())
        self.fighters.clear()
        for f in fs:
            s = f.s
            s.fighting = False
            s.fight_move = ""
            if s.state != State.GRAPPLE:
                s.drop_rod()
            s.target_x = None
            s.running = False
            if s.state == State.IDLE:
                s.idle_for = s.state_time + random.uniform(1.0, 2.5)
        live = [f for f in fs if f.s in self.app.world.stickmen]
        if not quiet and len(live) >= 2:
            best = min(live, key=lambda f: (f.hits - f.dealt, f.hits))
            others = [f for f in live if f is not best]
            if best.hits < min(f.hits for f in others):
                best.s.jump()
                best.s.wave(2.0)
                self.app.director.note_event(tr("fight.won", name=best.s.name, hits=best.dealt))
            else:
                self.app.director.note_event(tr("fight.draw"))
        elif not quiet:
            self.app.director.note_event(tr("fight.end"))
        return True

    # ------------------------------------------------------------ the fight
    def update(self) -> None:
        now = time.monotonic()
        self.sparks = [sp for sp in self.sparks if now - sp[2] < SPARK_TIME]
        if not self.active:
            return
        alive = self.app.world.stickmen
        for k in [k for k, f in self.fighters.items() if f.s not in alive or f.s.leaving]:
            self.fighters.pop(k).s.fighting = False
        if not self.fighters or now > self.until:
            self.stop()
            return
        for f in list(self.fighters.values()):
            self._think(f, now)

    def _opponent(self, f: Fighter) -> Fighter | None:
        others = [o for o in self.fighters.values() if o is not f and o.s.state != State.DRAGGED]
        if not others:
            return None
        return min(others, key=lambda o: abs(o.s.pos.x - f.s.pos.x) + 3 * abs(o.s.pos.y - f.s.pos.y))

    def _think(self, f: Fighter, now: float) -> None:
        s = f.s
        if s.state == State.DRAGGED:
            return
        opp = self._opponent(f)
        u = s.fight_progress()

        # a flying kick connects in the air
        if s.fight_move == "jump_kick" and s.state in BUSY_AIR:
            if opp is not None and not f.landed:
                dx, dy = opp.s.pos.x - s.pos.x, opp.s.pos.y - s.pos.y
                if abs(dx) < 0.75 * s.h and abs(dy) < 0.9 * s.h:
                    f.landed = True
                    self._strike(f, opp, "jump_kick")
            return
        if s.state in BUSY_AIR or s.state in (State.CLIMB_PREP, State.CLIMB_JUMP, State.CLIMBING, State.MANTLE):
            return
        if s.state == State.SITTING:
            s.sit_until = s.time

        if opp is None and f.rod is not None:
            s.drop_rod()
            f.rod = None
        if opp is None:  # alone: shadow boxing
            if u < 0 and now > f.next_attack and s.state == State.IDLE:
                s.fight_do(random.choice(("punch", "punch", "kick")), PUNCH_TIME)
                f.next_attack = now + random.uniform(0.4, 1.0)
            return

        o = opp.s
        if f.rod is not None:
            self._rod_step(f, opp, now)
            return
        dist = o.pos.x - s.pos.x
        d = abs(dist) / s.h
        toward = 1 if dist >= 0 else -1
        same_level = abs(o.pos.y - s.pos.y) < 0.5 * s.h

        # running away for a moment (sometimes up a window with the fishing rod)
        if now < f.flee_until and not getattr(f, "fled_up", False) and s.grounded and random.random() < 0.5:
            f.fled_up = True
            if s.try_grapple(self.app.scene, up_only=True):
                return
        if now < f.flee_until:
            away = self._clamp_x(s, s.pos.x - toward * 5 * s.h)
            if s.target_x is None or abs(s.target_x - away) > s.h:
                s.go_to(away, max_seconds=3.0, run=True)
            return
        if f.flee_until and now >= f.flee_until:
            f.flee_until = 0.0
            f.fled_up = False
            s.target_x = None

        # an attack in progress: does it land?
        if u >= 0 and s.fight_move in ("punch", "kick"):
            hit_from, hit_to = (0.32, 0.62) if s.fight_move == "punch" else (0.38, 0.72)
            if not f.landed and hit_from <= u <= hit_to:
                reach = REACH_PUNCH if s.fight_move == "punch" else REACH_KICK
                if same_level and dist * s.facing > 0 and d <= reach:
                    f.landed = True
                    self._strike(f, opp, s.fight_move)
            return
        if u >= 0:  # blocking / staggering: let it play out
            return

        if not same_level:  # one is up on a window, the other on the floor: jump over to him
            if s.grounded and o.grounded and o.support is not None and now > f.regather:
                f.regather = now + 2.0
                sp = o.support
                x = clamp(o.pos.x - toward * CLOSE * s.h, sp.x0 + 0.2 * s.h, sp.x1 - 0.2 * s.h)
                s.target_x = None
                s.leap_to(sp, x)
            return

        if (ROD_MIN <= d <= ROD_MAX and now >= f.rod_ready and o.grounded and o.state != State.GRAPPLE
                and s.state in (State.IDLE, State.LANDING, State.WALKING) and random.random() < f.rod_p * 0.04):
            s.target_x = None
            s.running = False
            s.speed = 0.0
            s.idle_for = 1e9
            s.set_state(State.IDLE)
            self._rod_cast(f, opp)  # hook him from here and pull him in
            return

        if s.state == State.WALKING and s.target_x is not None:
            # charging in; from a few steps away, maybe a flying kick
            if same_level and 1.9 <= d <= 3.0 and o.grounded and random.random() < f.flying_p * 0.04:
                self._flying_kick(f, toward)
                return
            if d <= CLOSE + 0.2:
                s.target_x = None
                s.running = False
                s.speed *= 0.3
                s.idle_for = 1e9
                s.set_state(State.IDLE)
            else:
                s.target_x = self._clamp_x(s, o.pos.x - toward * CLOSE * s.h)
            return
        if s.state not in (State.IDLE, State.LANDING):
            return
        s.facing = toward
        if d > 1.5:  # far: walk / run over
            s.go_to(self._clamp_x(s, o.pos.x - toward * CLOSE * s.h), max_seconds=4.0, run=d > 2.2)
            return
        if d > REACH_PUNCH - 0.08:  # almost there: a quick step in (walking can't take steps this short)
            if abs(s.shove) < 0.3 * s.h:
                s.shove = toward * min(3.0, 1.2 + 2.5 * (d - CLOSE)) * s.h
            return
        if d < 0.40:  # too close: hop back a little
            s.shove = -toward * 2.2 * s.h
        if now < f.next_attack:
            if not s.shove and random.random() < 0.025:  # footwork: little steps in and out
                s.shove = random.choice((-1, 1)) * toward * 0.9 * s.h
            return
        # attack!
        if f.combo > 0:
            move = "punch"
            f.combo -= 1
            f.next_attack = now + PUNCH_TIME * 0.9
        else:
            move = "kick" if (d > 0.5 and random.random() < 0.35) else "punch"
            if move == "punch" and random.random() < 0.3:
                f.combo = random.randint(1, 2)
            f.next_attack = now + random.uniform(0.35, 1.0) / f.aggression
        f.landed = False
        s.fight_do(move, PUNCH_TIME if move == "punch" else KICK_TIME)
        self._maybe_block(opp, s, move)

    # ---- the fishing rod as a weapon: hook the opponent and yank him over
    def _rod_cast(self, f: Fighter, opp: Fighter) -> None:
        s, o = f.s, opp.s
        s.facing = 1 if o.pos.x >= s.pos.x else -1
        s.rod_on, s.hook_taut = True, False
        s.rod_grip = s.joints.get("hand1", s.pos).copy()
        s.hook = s.rod_grip.copy()
        fly = 0.12 + abs(o.pos.x - s.pos.x) / (16.0 * s.h)
        f.rod = {"t0": time.monotonic(), "fly": fly, "from": s.hook.copy(), "pull_until": 0.0}
        f.rod_ready = time.monotonic() + random.uniform(4.0, 8.0)
        s.fight_do("rod", 0.2 + fly + 0.1)
        f.next_attack = time.monotonic() + 0.8

    @staticmethod
    def _cancel_rod(f: Fighter) -> None:
        """Hit or pulled while casting: the rod is dropped."""
        if f.rod is not None:
            f.rod = None
            f.s.drop_rod()
            if f.s.fight_move in ("rod", "rod_pull"):
                f.s.fight_move = ""

    def _rod_step(self, f: Fighter, opp: Fighter, now: float) -> None:
        s, o, r = f.s, opp.s, f.rod
        chest = o.pos + Vec(0, -0.62 * o.h)
        if r["pull_until"]:  # yanking: the hook stays in him until he's pulled in
            s.hook = chest
            if now > r["pull_until"] or abs(o.pos.x - s.pos.x) < 0.9 * s.h:
                s.drop_rod()
                f.rod = None
                f.next_attack = now + 0.1  # and a punch to welcome him
            return
        u = (now - r["t0"] - 0.2) / r["fly"]
        if u < 0:
            s.hook = s.rod_grip.copy()
            return
        u = min(u, 1.0)
        a = r["from"]
        arc = (chest - a).length() * 0.10 * math.sin(math.pi * u)
        s.hook = Vec(a.x + (chest.x - a.x) * u, a.y + (chest.y - a.y) * u - arc)
        if u < 1.0:
            return
        toward = 1 if o.pos.x >= s.pos.x else -1
        if o.fight_move == "block" and o.fight_progress() >= 0 and o.facing == -toward:
            self.sparks.append((chest.x, chest.y, now, o.h, True))  # knocked the hook away
            s.drop_rod()
            f.rod = None
            return
        # hooked! pull him over (off his feet), toward the caster
        self._cancel_rod(opp)
        self.sparks.append((chest.x, chest.y, now, o.h * 0.7, False))
        s.hook_taut = True
        s.fight_do("rod_pull", 0.5)
        r["pull_until"] = now + 0.7
        dist = abs(o.pos.x - s.pos.x)
        o.target_x = None
        o.running = False
        o.facing = -toward
        o.fight_do("hit", 1.0)
        o.start_fall(Vec(-toward * min(9.0, 2.2 * dist / s.h + 2.0) * o.h, -3.2 * o.h))
        o.jumping = False
        f.dealt += 1
        opp.hits += 1

    def _maybe_block(self, opp: Fighter, attacker, move: str) -> None:
        o = opp.s
        if o.state not in (State.IDLE, State.LANDING) or o.fight_progress() >= 0 or time.monotonic() < opp.flee_until:
            return
        if random.random() < opp.block_p:
            o.facing = 1 if attacker.pos.x >= o.pos.x else -1
            o.fight_do("block", BLOCK_TIME if move != "kick" else BLOCK_TIME + 0.15)

    def _flying_kick(self, f: Fighter, toward: int) -> None:
        s = f.s
        s.target_x = None
        s.running = False
        s.facing = toward
        f.landed = False
        s.fight_do("jump_kick", 1.2)
        s.squash_v -= 4.5
        s.start_fall(Vec(toward * 5.4 * s.h, -4.4 * s.h))
        s.jumping = True
        f.next_attack = time.monotonic() + random.uniform(0.6, 1.2)

    def _strike(self, f: Fighter, opp: Fighter, move: str) -> None:
        s, o = f.s, opp.s
        toward = 1 if o.pos.x >= s.pos.x else -1
        blocking = o.fight_move == "block" and o.fight_progress() >= 0 and o.facing == -toward
        head_y = o.pos.y - (0.80 if move == "punch" else 0.55) * o.h
        x = o.pos.x - toward * 0.12 * o.h
        if blocking:
            self.sparks.append((x, head_y, time.monotonic(), o.h, True))
            o.shove = toward * (2.0 if move == "punch" else 3.2) * o.h
            if move == "jump_kick":  # bounces off the guard
                s.vel = Vec(-toward * 2.5 * s.h, min(s.vel.y, -1.5 * s.h))
            else:
                s.shove = -toward * 0.8 * s.h
            return
        self.sparks.append((x, head_y, time.monotonic(), o.h, False))
        self._cancel_rod(opp)
        f.dealt += 1
        opp.hits += 1
        opp.streak += 1
        o.target_x = None
        o.running = False
        o.facing = -toward
        if move == "punch" and opp.streak % 3 != 0:
            o.fight_do("hit", 0.45)  # staggers back a step
            o.shove = toward * 2.6 * o.h
            if o.state != State.IDLE:
                o.idle_for = 1e9
                o.set_state(State.IDLE)
        else:  # kick, flying kick or the third punch: off his feet
            o.fight_do("hit", 1.4)
            power = 1.4 if move == "jump_kick" else 1.0
            o.start_fall(Vec(toward * 4.2 * power * o.h, -3.0 * o.h))
            o.jumping = False
        opp.next_attack = time.monotonic() + random.uniform(0.4, 0.9)
        if opp.streak >= 3 and random.random() < opp.flee_p * 2:
            opp.streak = 0
            opp.flee_until = time.monotonic() + random.uniform(1.6, 2.8)

    def _clamp_x(self, s, x: float) -> float:
        a = self.arena
        if a is None or not s.support or not s.support.is_floor:
            return x
        return clamp(x, a.x0 + 0.6 * s.h, a.x1 - 0.6 * s.h)

    # ------------------------------------------------------------ drawing
    def bbox(self) -> Rect | None:
        rects = [Rect(int(x - 0.5 * h), int(y - 0.5 * h), int(x + 0.5 * h), int(y + 0.5 * h))
                 for x, y, _t, h, _b in self.sparks]
        for f in self.fighters.values():
            if f.s.fight_move in ("kick", "jump_kick"):
                b = f.s.bbox()
                rects.append(Rect(b.left - int(0.4 * f.s.h), b.top, b.right + int(0.4 * f.s.h), b.bottom))
        if not rects:
            return None
        r = rects[0]
        for x in rects[1:]:
            r = r.union(x)
        return r

    def paint(self, p: QPainter) -> None:
        now = time.monotonic()
        p.save()
        # motion smear behind a kicking leg (like a cartoon swoosh)
        for f in self.fighters.values():
            s = f.s
            u = s.fight_progress()
            if s.fight_move not in ("kick", "jump_kick") or u < 0 or "foot1" not in s.joints:
                continue
            if s.fight_move == "jump_kick" and s.state not in BUSY_AIR:
                continue
            if s.fight_move == "kick" and not 0.3 <= u <= 0.75:
                continue
            c = QColor(s.color)
            hip, foot = s.joints["hip"], s.joints["foot1"]
            for i, a in enumerate((70, 40, 20)):
                c.setAlpha(a)
                back = (i + 1) * 0.07 * s.h
                p.setPen(QPen(c, s.stroke * (1.6 - 0.35 * i), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(hip.x - s.facing * back, hip.y + back * 0.4),
                           QPointF(foot.x - s.facing * back, foot.y + back * 0.6))
        # impact bursts: yellow-white for a hit, pale blue for a block
        for x, y, t0, h, blocked in self.sparks:
            k = (now - t0) / SPARK_TIME
            r0, r1 = (0.10 + 0.25 * k) * h, (0.22 + 0.32 * k) * h
            c = QColor(140, 200, 255) if blocked else QColor(255, 214, 64)
            c.setAlpha(int(255 * (1.0 - k)))
            p.setPen(QPen(c, max(2.0, 0.035 * h * (1.0 - 0.5 * k)), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            n = 6 if blocked else 9
            for i in range(n):
                a = 2 * math.pi * i / n + t0
                p.drawLine(QPointF(x + math.cos(a) * r0, y + math.sin(a) * r0),
                           QPointF(x + math.cos(a) * r1, y + math.sin(a) * r1))
            if not blocked:
                c2 = QColor(255, 255, 255)
                c2.setAlpha(int(200 * (1.0 - k)))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c2)
                p.drawEllipse(QPointF(x, y), 0.07 * h * (1.0 - k), 0.07 * h * (1.0 - k))
        p.restore()
