"""One procedurally animated stickman.

Body model
----------
`pos` is the contact point under the feet, in physical desktop pixels.
Each frame we:
  1. run the physics / behaviour state machine,
  2. compute a *target pose* (hip offset, pelvis lean, foot and hand targets),
  3. smooth the pose toward that target. Different body parts smooth at
     different rates (pelvis -> chest -> head), which gives overlapping
     action / follow-through for free and blends every state change,
  4. solve two-bone IK for knees and elbows,
  5. turn each chain into a quadratic Bezier that passes *through* the
     joint, so limbs and spine are drawn as smooth rubber-hose curves
     rather than straight bones.

States
------
FALLING, LANDING, IDLE, WALKING, TURNING   - basic locomotion
CLIMB_PREP -> CLIMB_JUMP -> CLIMBING -> MANTLE  - scaling a window's side
DRAGGED                                    - held by the mouse (pendulum)
"""
from __future__ import annotations

import math
import random
from enum import Enum, auto
from typing import TYPE_CHECKING

from ..desktop import win32
from ..desktop.scene import Platform, Wall
from ..desktop.win32 import Rect
from .mathutil import (
    Vec, clamp, damp, damp_vec, dist_to_segment, ease_out_cubic, lerp, normalize, quad_point,
    rotate, smoothstep, solve_two_bone, through_control, up_dir,
)

if TYPE_CHECKING:
    from ..desktop.scene import DesktopScene

# Tunables, in body-heights (h) so every size behaves the same.
GRAVITY = 14.0          # h/s^2   (cartoon gravity, ~2.5x real)
MAX_FALL = 16.0         # h/s
WALK_SPEED = 0.95       # h/s
WALK_ACCEL = 2.5        # h/s^2
STRIDE = 0.42           # h
EDGE_HOP = 0.9          # h/s     little pop when stepping off a ledge
CLIMB_SPEED = 0.55      # h/s
CLIMB_STRIDE = 0.30     # h       hand-over-hand reach
WALL_GAP = 0.19         # h       body distance from the wall while climbing
WALL_DETECT = 0.30      # h       how close a wall must be to start climbing
MAX_REACH_UP = 1.25     # h       window bottoms higher than this can't be grabbed
MANTLE_TIME = 0.6       # s
MAX_THROW = 30.0        # h/s
CLIMB_CHANCE = 0.85
RUN_FACTOR = 2.4        # running top speed = walk_speed * this
RUN_ACCEL = 7.0         # h/s^2
JUMP_UP = 5.2           # h/s     take-off speed of a deliberate jump
JUMP_PREP_TIME = 0.17   # s       anticipation crouch before a jump


class State(Enum):
    FALLING = auto()
    LANDING = auto()
    IDLE = auto()
    WALKING = auto()
    TURNING = auto()
    CLIMB_PREP = auto()
    CLIMB_JUMP = auto()
    CLIMBING = auto()
    MANTLE = auto()
    DRAGGED = auto()
    SITTING = auto()
    JUMP_PREP = auto()
    HOP = auto()        # aimed jump onto a widget (search bar...)


GROUND_STATES = frozenset({State.LANDING, State.IDLE, State.WALKING, State.TURNING,
                           State.SITTING, State.JUMP_PREP})
CLIMB_STATES = frozenset({State.CLIMB_PREP, State.CLIMB_JUMP, State.CLIMBING, State.MANTLE})

# Limb names -> (root, middle, end) joint keys; index 0 is the far side.
LIMBS = (("arm0", "shoulder", "elbow0", "hand0"), ("leg0", "hip", "knee0", "foot0"),
         ("leg1", "hip", "knee1", "foot1"), ("arm1", "shoulder", "elbow1", "hand1"))


class Stickman:
    def __init__(self, name: str, color: str, height: float, x: float, y: float, facing: int = 1):
        self._set_dims(height)
        h = height
        self.name, self.color = name, color

        # personality-driven physical traits (overridden from characters.toml)
        self.key = name.lower()
        self.born = 0.0            # time.monotonic() at spawn (set by World)
        self.walk_speed = WALK_SPEED
        self.climb_chance = CLIMB_CHANCE
        self.restlessness = 1.0
        self.energy = 1.0          # >1 runs and jumps more, <1 sits around more
        self.running = False
        self.run_until = 0.0
        self.sit_until = 0.0
        self.sit_edge = False      # sitting on a window's edge, legs dangling
        self.sit_on_arrival = False
        self.jumping = False       # airborne from a deliberate jump (jump pose)
        self.hop_target: Platform | None = None
        self.hop_time = 0.6
        # brain-driven extras
        self.gesture = ""          # "wave" while active
        self.gesture_until = 0.0
        self.leaving = False       # jumping off the top of the screen for good
        self.target_x: float | None = None  # walking over to someone
        self.target_until = 0.0

        self.pos = Vec(x, y)
        self.vel = Vec()
        self.facing = facing
        self.state = State.FALLING
        self.state_time = 0.0
        self.time = random.random() * 10
        self.speed = 0.0
        self.phase = 0.0
        self.climb_phase = 0.0
        self.squash = 0.0
        self.squash_v = 0.0
        self.idle_for = 0.0
        self._turned = False

        # what we're attached to
        self.support: Platform | None = None
        self.wall: Wall | None = None
        self.attach_rect: Rect | None = None
        self.support_changed = False
        self.cooldown: dict[int, float] = {}  # hwnd -> time before we may climb it again
        self.jump_from = Vec()
        self.jump_to = Vec()
        self.jump_time = 0.3
        self.mantle_from = Vec()

        # dragging
        self.anchor = Vec()
        self.drag_target = Vec()
        self.drag_vel = Vec()
        self.hang = Vec()
        self.hang_prev = Vec()
        self.hang_angle = 0.0

        # smoothed pose
        self.p_hip = Vec(0, -self.leg * 0.92)
        self.p_lean = 0.0   # pelvis
        self.p_chest = 0.0  # lags the pelvis -> spine curves
        self.p_head = 0.0   # lags the chest -> head follows through
        self.p_facing = float(facing)
        self.p_feet = [Vec(-0.07 * h, 0), Vec(0.07 * h, 0)]
        self.p_hands = [Vec(-0.05 * h, 0.28 * h), Vec(0.05 * h, 0.28 * h)]
        self.joints: dict[str, Vec] = {}
        self.curves: dict[str, tuple[Vec, Vec, Vec]] = {}
        self.foot_targets: list[Vec] = []
        self._build_joints()

    def _set_dims(self, h: float) -> None:
        self.h = h
        # proportions: big round head, short torso, long curvy limbs
        self.thigh = self.shin = 0.235 * h
        self.leg = self.thigh + self.shin
        self.torso = 0.27 * h
        self.head_r = 0.12 * h
        self.upper_arm, self.forearm = 0.165 * h, 0.16 * h
        self.stroke = 0.058 * h

    def set_height(self, h: float) -> None:
        """Grow/shrink live; the smoothed pose eases into the new size."""
        if abs(h - self.h) < 0.5:
            return
        k = h / self.h
        self._set_dims(h)
        self.p_hip = self.p_hip * k
        self.p_feet = [v * k for v in self.p_feet]
        self.p_hands = [v * k for v in self.p_hands]
        self.squash_v -= 2.0  # a little boing

    # ------------------------------------------------------------------ state
    @property
    def grounded(self) -> bool:
        return self.state in GROUND_STATES

    def set_state(self, state: State) -> None:
        self.state = state
        self.state_time = 0.0
        if state == State.TURNING:
            self._turned = False

    def _detach(self) -> None:
        if self.support is not None or self.wall is not None:
            self.support_changed = True
        self.support = None
        self.wall = None
        self.attach_rect = None

    def start_fall(self, vel: Vec | None = None) -> None:
        self.vel = vel if vel is not None else Vec(self.facing * self.speed * 0.6, 0.0)
        self._detach()
        self.set_state(State.FALLING)

    def land(self, p: Platform) -> None:
        self.jumping = False
        impact = clamp(self.vel.y / (self.h * 9.0), 0.12, 1.0)
        self.pos.y = p.y
        self._detach()
        self.support = p
        self.support_changed = True
        self.speed = min(abs(self.vel.x), self.walk_speed * self.h) * 0.5
        self.vel = Vec()
        self.squash_v += 7.5 * impact
        self.set_state(State.LANDING)

    def respawn(self, x: float, y: float) -> None:
        self.pos = Vec(x, y)
        self.speed = 0.0
        self.start_fall(Vec(0, 0))

    # ------------------------------------------------------- brain actions
    def talk(self, seconds: float) -> None:
        """Stand still while speaking (only interrupts walking; sitters stay seated)."""
        if self.state == State.IDLE:
            self.idle_for = max(self.idle_for, self.state_time + seconds)
        elif self.state == State.WALKING and self.target_x is None:
            self.running = False
            self.idle_for = seconds
            self.set_state(State.IDLE)
        elif self.state == State.SITTING:
            self.sit_until = max(self.sit_until, self.time + seconds)

    def face_toward(self, x: float) -> None:
        if self.state in (State.IDLE, State.LANDING) and abs(x - self.pos.x) > 2:
            self.facing = 1 if x > self.pos.x else -1  # pose smoothing turns him around

    def go_to(self, x: float, max_seconds: float = 10.0, run: bool | None = None) -> None:
        """Walk (or run, if far) over to x on the current surface; gives up after a while."""
        if not self.grounded or abs(x - self.pos.x) < 0.6 * self.h:
            self.face_toward(x)
            return
        self.target_x, self.target_until = x, self.time + max_seconds
        self.facing = 1 if x > self.pos.x else -1
        if run is None:
            run = abs(x - self.pos.x) > 4 * self.h
        if run:
            self.run(max_seconds)
        self.set_state(State.WALKING)

    def wave(self, seconds: float = 2.4) -> None:
        self.gesture, self.gesture_until = "wave", self.time + seconds
        self.talk(seconds)

    def jump(self) -> None:
        """Crouch, leap, tuck, land: a deliberate hop (forward if moving)."""
        if self.grounded and self.state not in (State.JUMP_PREP,):
            self.hop_target = None
            self.set_state(State.JUMP_PREP)

    def _launch(self) -> None:
        h = self.h
        vx = self.facing * (self.speed * 0.8 + 0.3 * h)
        self.squash_v -= 4.5  # stretch on take-off
        self.start_fall(Vec(vx, -JUMP_UP * h * (0.9 + 0.2 * random.random())))
        self.jumping = True

    def run(self, seconds: float = 4.0) -> None:
        self.running, self.run_until = True, self.time + seconds
        if self.state in (State.IDLE, State.SITTING, State.LANDING):
            self.set_state(State.WALKING)

    def sit(self, seconds: float = 8.0) -> None:
        """Sit down right here. On a window's top edge or a widget (anything
        but the taskbar floor) the legs hang down over the front, swinging."""
        if not self.grounded or self.state in (State.SITTING, State.JUMP_PREP):
            return
        sp = self.support
        self._sit_now(seconds, edge=sp is not None and not sp.is_floor)

    def _sit_now(self, seconds: float, edge: bool) -> None:
        self.sit_on_arrival = False
        self.target_x = None
        self.running = False
        self.speed = 0.0
        self.sit_edge = edge
        self.sit_until = self.time + seconds
        self.set_state(State.SITTING)

    def leave(self) -> None:
        """Leap off the top of the screen; the world removes him once gone."""
        self.leaving = True
        self.gesture = ""
        self.squash_v -= 4.0
        self.start_fall(Vec(self.facing * 0.4 * self.h, -26.0 * self.h))

    # ---------------------------------------------------------------- input
    def hit_test(self, x: float, y: float, slack: float = 0.0) -> bool:
        tol = self.stroke * 0.5 + slack
        hd = self.joints["head"]
        if math.hypot(x - hd.x, y - hd.y) <= self.head_r + tol:
            return True
        for a, c, b in self.curves.values():
            prev = a
            for i in range(1, 7):
                cur = quad_point(a, c, b, i / 6)
                if dist_to_segment(x, y, prev, cur) <= tol:
                    return True
                prev = cur
        return False

    def grab(self, x: float, y: float) -> None:
        self._detach()
        self.target_x = None
        self.hop_target = None
        self.set_state(State.DRAGGED)
        self.anchor = self.joints["head"].copy()
        self.drag_target = Vec(x, y)
        self.drag_vel = Vec()
        self.hang = self.joints["hip"].copy()
        self.hang_prev = self.hang.copy()
        self.speed = 0.0
        self.squash = self.squash_v = 0.0

    def release(self) -> None:
        if self.state != State.DRAGGED:
            return
        v = self.drag_vel
        n = v.length()
        cap = MAX_THROW * self.h
        if n > cap:
            v = v * (cap / n)
        if abs(v.x) > 0.5 * self.h:
            self.facing = 1 if v.x > 0 else -1
        self.start_fall(v)

    # --------------------------------------------------------------- update
    def update(self, dt: float, scene: "DesktopScene", pose_dt: float | None = None,
               first: bool = True) -> None:
        """One physics substep. The (expensive) pose + skeleton is rebuilt only
        when pose_dt is given, i.e. once per rendered frame, not per substep."""
        self._frame_first = first
        self.time += dt
        self.state_time += dt
        st = self.state
        if st == State.DRAGGED:
            self._update_dragged(dt)
        elif st == State.FALLING:
            self._update_falling(dt, scene)
        elif st == State.HOP:
            self._update_hop(dt, scene)
        elif st in CLIMB_STATES:
            self._update_climb(dt, scene)
        else:
            self._update_grounded(dt, scene)

        # landing squash: under-damped spring -> one soft bounce
        k = 240.0
        c = 2.0 * math.sqrt(k) * 0.42
        self.squash_v += (-k * self.squash - c * self.squash_v) * dt
        self.squash = clamp(self.squash + self.squash_v * dt, -0.25, 1.0)

        if pose_dt is not None:
            self._update_pose(pose_dt)
            self._build_joints()
            if self.state == State.DRAGGED:
                self._pin_head_to_anchor()

    # ---- attachment to moving windows
    def _follow_window(self, hwnd: int) -> tuple[int, int] | None:
        """Track a window's live rect; returns its movement since last frame,
        or None if it vanished (closed, minimized, cloaked)."""
        if hwnd < 0:
            return 0, 0  # app-made static surface (couch): never moves
        if not getattr(self, "_frame_first", True) and self.attach_rect is not None:
            return 0, 0  # already checked the window this frame (saves a system call per substep)
        live = win32.query_live_rect(hwnd)
        if live is None:
            return None
        old, self.attach_rect = self.attach_rect, live
        if old is None or old == live:
            return 0, 0
        return live.left - old.left, live.top - old.top

    def _shift(self, dx: int, dy: int) -> None:
        d = Vec(dx, dy)
        self.pos = self.pos + d
        self.jump_from, self.jump_to, self.mantle_from = self.jump_from + d, self.jump_to + d, self.mantle_from + d
        if self.support is not None:
            self.support = self.support.shifted(dx, dy)
            self.support_changed = True
        if self.wall is not None:
            self.wall = self.wall.shifted(dx, dy)
            self.support_changed = True

    def resync_support(self, scene: "DesktopScene") -> None:
        """After each scene poll: is the surface we're on still visible?

        Windows opened on top (or moved over us) hide parts of edges, so a
        standing stickman loses his platform and a climber loses his wall.
        """
        if self.state in CLIMB_STATES:
            self._resync_wall(scene)
            return
        if not self.grounded or self.support is None:
            return
        p = scene.platform_under(self.pos.x, self.pos.y, self.support.hwnd, window_tolerance=0.5 * self.h)
        if p is None:
            self.start_fall()  # window closed, or another window now covers our spot
        else:
            if p != self.support:
                self.support_changed = True
            self.support = p
            self.pos.y = p.y

    def _resync_wall(self, scene: "DesktopScene") -> None:
        w = self.wall
        if w is None:
            return
        h = self.h
        # The part of the wall we need right now: where our hands grip it while
        # climbing (feet may dangle a little below a window's bottom edge), or
        # just the top corner while crouching to jump / mantling over.
        if self.state == State.CLIMBING:
            need0, need1 = self.pos.y - 0.92 * h, self.pos.y - 0.6 * h
        elif self.state == State.CLIMB_JUMP:
            need0, need1 = self.jump_to.y - 0.92 * h, self.jump_to.y - 0.6 * h
        else:  # CLIMB_PREP / MANTLE: only the top corner matters
            need0 = need1 = w.y0
        # A climbable piece always starts at the window's top corner, so it is
        # enough that it reaches down to our lowest contact point.
        for cand in scene.walls:
            if (cand.hwnd == w.hwnd and cand.side == w.side and abs(cand.x - w.x) <= 2
                    and cand.climbable and cand.y0 <= need0 + h and need1 <= cand.y1 + 1):
                self.wall = cand
                return
        # Our stretch of wall (or the top we were heading for) is now hidden
        # behind another window: let go.
        self.cooldown[w.hwnd] = self.time + 3.0
        self.start_fall(Vec(-self.facing * 0.3 * h, 0.0))

    # ---- falling / thrown
    def _update_falling(self, dt: float, scene: "DesktopScene") -> None:
        h = self.h
        self.vel.y = min(self.vel.y + GRAVITY * h * dt, MAX_FALL * h)
        self.vel.x *= math.exp(-0.5 * dt)
        y0 = self.pos.y
        nx, ny = self.pos.x + self.vel.x * dt, self.pos.y + self.vel.y * dt
        # thrown above the screen top still counts as "inside" horizontally
        probe_y = clamp(min(y0, ny), scene.highest_top, scene.lowest_floor)
        if not scene.in_work_area(nx, probe_y):
            nx, self.vel.x = self.pos.x, -self.vel.x * 0.25  # bump off the screen edge
        if self.vel.y > 0 and not self.leaving:
            p = scene.landing(nx, y0, ny)
            if p is None:
                fl = scene.floor_under(nx, y0)
                if fl is not None and ny >= fl.y:
                    p = fl  # safety net: started below a floor (dropped over the taskbar)
            if p is not None:
                self.pos.x = nx
                self.land(p)
                return
        self.pos = Vec(nx, ny)

    # ---- walking
    def _update_grounded(self, dt: float, scene: "DesktopScene") -> None:
        sp = self.support
        if sp is None:
            self.start_fall()
            return
        if not sp.is_floor:
            d = self._follow_window(sp.hwnd)
            if d is None:
                self.start_fall()
                return
            if d != (0, 0):
                self._shift(*d)
        h, st = self.h, self.state
        if self.running and self.time > self.run_until:
            self.running = False
        top = self.walk_speed * h * (RUN_FACTOR if self.running else 1.0)

        if st == State.LANDING:
            self.speed = damp(self.speed, 0.0, 5.0, dt)
            if self.state_time > 0.45:
                self.set_state(State.WALKING)
        elif st == State.IDLE:
            self.speed = damp(self.speed, 0.0, 8.0, dt)
            if self.state_time > self.idle_for:
                self.set_state(State.WALKING)
        elif st == State.SITTING:
            self.speed = 0.0
            if self.time > self.sit_until:
                self.idle_for = 0.5  # stand up (the pose eases up), then go
                self.set_state(State.IDLE)
        elif st == State.JUMP_PREP:
            self.speed = damp(self.speed, self.speed * 0.6, 6.0, dt)
            if self.state_time > JUMP_PREP_TIME:
                if self.hop_target is not None:
                    self._start_hop()
                else:
                    self._launch()
                return
        elif st == State.WALKING:
            accel = (RUN_ACCEL if self.running else WALK_ACCEL) * h
            if self.speed < top:
                self.speed = min(self.speed + accel * dt, top)
            else:  # slowing from a run back to a walk
                self.speed = max(top, self.speed - 3.0 * h * dt)
            if self.target_x is not None:  # walking over to someone / somewhere
                dx = self.target_x - self.pos.x
                if abs(dx) < 0.5 * h or self.time > self.target_until:
                    self.target_x = None
                    self.running = False
                    if self.sit_on_arrival:
                        self._sit_now(self.sit_until, edge=abs(dx) < 0.5 * h)
                    else:
                        self.idle_for = random.uniform(2.0, 4.0)
                        self.set_state(State.IDLE)
                elif (dx > 0) != (self.facing > 0):
                    self.set_state(State.TURNING)
            else:
                # spontaneous behaviour, flavoured by personality
                lazy = max(0.0, 2.0 - self.energy)
                roll = random.random()
                if roll < dt * self.restlessness / 9.0:  # stop and look around
                    self.idle_for = random.uniform(0.8, 2.2)
                    self.set_state(State.IDLE)
                elif roll < dt * (self.restlessness / 9.0 + lazy / 40.0):
                    self.sit(random.uniform(5.0, 12.0) * (0.6 + 0.4 * lazy))
                elif roll < dt * (self.restlessness / 9.0 + lazy / 40.0 + self.energy / 30.0):
                    self.run(random.uniform(1.5, 4.0))
                elif roll < dt * (self.restlessness / 9.0 + lazy / 40.0 + self.energy / 18.0):
                    self.jump()
                elif roll < dt * (self.restlessness / 9.0 + lazy / 40.0 + self.energy / 18.0 + 0.08):
                    self.hop_to_widget(scene)
        elif st == State.TURNING:
            self.speed = max(0.0, self.speed - 6.0 * h * dt)
            if not self._turned and self.state_time > 0.18:
                self.facing = -self.facing
                self._turned = True
            if self.state_time > 0.45:
                self.set_state(State.WALKING)

        if self.state == State.WALKING and self.target_x is None:
            wall = self._find_wall(scene)
            if wall is not None:
                if random.random() < self.climb_chance:
                    self._begin_climb(wall)
                    return
                self.cooldown[wall.hwnd] = self.time + 4.0  # walk past this one

        if self.speed > 1e-3:
            nx = self.pos.x + self.facing * self.speed * dt
            stop_dist = self.speed * self.speed / (2 * 6.0 * h)  # TURNING decelerates at 6 h/s^2
            ahead = nx + self.facing * (0.25 * h + stop_dist)
            if self.state == State.WALKING and not scene.in_work_area(ahead, self.pos.y - 2):
                self.set_state(State.TURNING)  # screen edge: turn around
            else:
                self.pos.x = nx
                sp = self.support
                if sp is not None and not (sp.x0 <= nx <= sp.x1):
                    # walked off the end of a window: drop with momentum
                    self.start_fall(Vec(self.facing * self.speed, -EDGE_HOP * h))
                    return

        self.phase = (self.phase + self._gait_rate() * dt) % 1.0

    def _gait_mix(self) -> tuple[float, float]:
        """(walk amount 0..1, run amount 0..1) from the current speed."""
        walk = self.walk_speed * self.h
        r = clamp(self.speed / walk, 0.0, 1.0)
        run = clamp((self.speed - walk) / ((RUN_FACTOR - 1.0) * walk), 0.0, 1.0)
        return r, run

    def _gait_rate(self) -> float:
        """Strides per second. Stance length is derived from speed and rate, so
        the planted foot never slides, even while speeding up or slowing down."""
        r, run = self._gait_mix()
        base = self.walk_speed / (2.0 * STRIDE)
        return base * (0.35 + 0.65 * r) * (1.0 + 0.6 * run) if r > 0.01 else 0.0

    # ---- hopping onto widgets (search bars, address bars...)
    def hop_to_widget(self, scene: "DesktopScene") -> bool:
        """Pick a reachable widget top and jump onto it. True if a hop started."""
        if not self.grounded or self.state == State.JUMP_PREP:
            return False
        h, here = self.h, self.support
        options = []
        for p in scene.widget_targets():
            if here is not None and p == here:
                continue
            margin = 0.25 * h
            if p.x1 - p.x0 < 2 * margin + 0.1 * h:
                continue
            lx = min(max(self.pos.x + random.uniform(-0.8, 0.8) * h, p.x0 + margin), p.x1 - margin)
            dx, dy = lx - self.pos.x, p.y - self.pos.y  # dy > 0: target is lower
            if abs(dx) <= 4.5 * h and -1.7 * h <= dy <= 6.0 * h and (abs(dx) > 0.4 * h or abs(dy) > 0.25 * h):
                options.append((p, lx))
        if not options:
            return False
        self.hop_target, x = random.choice(options)
        self.jump_to = Vec(x, self.hop_target.y)
        self.facing = 1 if x >= self.pos.x else -1
        self.target_x = None
        self.running = False
        self.set_state(State.JUMP_PREP)
        return True

    def leap_to(self, target: Platform, x: float) -> bool:
        """Aimed jump onto any platform (used by tasks: onto a window to press
        its X, onto a field to type). Works from the ground or mid-air."""
        if self.state == State.DRAGGED or self.state in CLIMB_STATES:
            return False
        self.hop_target = target
        self.jump_to = Vec(x, target.y)
        self.facing = 1 if x >= self.pos.x else -1
        self.target_x = None
        self.running = False
        if self.grounded:
            self.set_state(State.JUMP_PREP)
        else:
            self._start_hop()
        return True

    def _start_hop(self) -> None:
        h = self.h
        self.jump_from = self.pos.copy()
        dist = abs(self.jump_to.x - self.jump_from.x) / h
        self.hop_time = 0.45 + 0.07 * dist + 0.04 * abs(self.jump_to.y - self.jump_from.y) / h
        self._detach()
        self.jumping = True
        self.squash_v -= 4.5
        self.set_state(State.HOP)

    def _update_hop(self, dt: float, scene: "DesktopScene") -> None:
        """Follow a parabola that lands exactly on the chosen widget, even if
        its window is being dragged around meanwhile."""
        t = self.hop_target
        if t is None:
            self.start_fall(Vec())
            return
        d = self._follow_window(t.hwnd)
        if d is None:
            self.hop_target = None
            self.start_fall(Vec())
            return
        if d != (0, 0):
            self._shift(*d)
            self.hop_target = t = t.shifted(*d)
        h = self.h
        u = clamp(self.state_time / self.hop_time, 0.0, 1.0)
        a, b = self.jump_from, self.jump_to
        rise = max(0.0, a.y - b.y)                # target above us -> jump higher
        arc = 0.55 * h + 0.6 * rise
        prev = self.pos
        self.pos = Vec(lerp(a.x, b.x, u), lerp(a.y, b.y, u) - 4.0 * arc * u * (1.0 - u))
        if dt > 0:
            self.vel = (self.pos - prev) * (1.0 / dt)  # drives the jump pose
        if u >= 1.0:
            p = scene.platform_under(b.x, b.y, t.hwnd, window_tolerance=0.3 * h) or t
            self.hop_target = None
            self.vel = Vec(self.vel.x, max(self.vel.y, 6.0 * h))
            rect = self.attach_rect
            self.land(p)
            self.attach_rect = rect  # keep tracking that window smoothly

    # ---- climbing
    def _find_wall(self, scene: "DesktopScene") -> Wall | None:
        f, h, fy = self.facing, self.h, self.pos.y
        own = self.support.hwnd if self.support is not None else None
        for w in scene.walls:
            if w.side != f or not w.climbable or w.hwnd == own:
                continue
            if not 0.0 <= (w.x - self.pos.x) * f <= WALL_DETECT * h:
                continue
            # top must be worth climbing, bottom must be within reach
            if w.y0 < fy - 0.35 * h and w.y1 >= fy - MAX_REACH_UP * h and self.cooldown.get(w.hwnd, 0) <= self.time:
                return w
        return None

    def _begin_climb(self, wall: Wall) -> None:
        self._detach()
        self.wall = wall
        self.support_changed = True
        self.set_state(State.CLIMB_PREP)

    def _update_climb(self, dt: float, scene: "DesktopScene") -> None:
        w = self.wall
        if w is None:
            self.start_fall(Vec())
            return
        d = self._follow_window(w.hwnd)
        if d is None:
            self.start_fall(Vec())
            return
        if d != (0, 0):
            self._shift(*d)
            w = self.wall
        h, f, st = self.h, self.facing, self.state

        if st == State.CLIMB_PREP:  # anticipation crouch
            self.speed = damp(self.speed, 0.0, 12.0, dt)
            if self.state_time > 0.2:
                self.jump_from = self.pos.copy()
                self.jump_to = Vec(w.x - f * WALL_GAP * h, min(self.pos.y, w.y1 + 0.08 * h))
                rise = max(0.0, self.jump_from.y - self.jump_to.y)
                self.jump_time = 0.24 + 0.22 * min(1.5, rise / h)
                self.squash_v -= 4.0  # stretch on take-off
                self.set_state(State.CLIMB_JUMP)
        elif st == State.CLIMB_JUMP:
            u = clamp(self.state_time / self.jump_time, 0.0, 1.0)
            a, b = self.jump_from, self.jump_to
            self.pos = Vec(lerp(a.x, b.x, smoothstep(u)),
                           lerp(a.y, b.y, ease_out_cubic(u)) - 0.10 * h * math.sin(math.pi * u))
            if u >= 1.0:
                self.climb_phase = 0.0
                self.set_state(State.CLIMBING)
        elif st == State.CLIMBING:
            self.pos = Vec(w.x - f * WALL_GAP * h, self.pos.y - CLIMB_SPEED * h * dt)
            self.climb_phase = (self.climb_phase + CLIMB_SPEED / (2 * CLIMB_STRIDE) * dt) % 1.0
            if self.pos.y - 0.62 * h <= w.y0:  # shoulders reached the top edge
                self.mantle_from = self.pos.copy()
                self.set_state(State.MANTLE)
        elif st == State.MANTLE:
            u = clamp(self.state_time / MANTLE_TIME, 0.0, 1.0)
            a = self.mantle_from
            self.pos = Vec(lerp(a.x, w.x + f * 0.24 * h, smoothstep((u - 0.3) / 0.7)),
                           lerp(a.y, w.y0, ease_out_cubic(u / 0.7)))
            if u >= 1.0:
                self.cooldown[w.hwnd] = self.time + 2.5
                p = scene.platform_under(self.pos.x, w.y0, w.hwnd)
                if p is None:
                    self.start_fall(Vec())
                    return
                rect = self.attach_rect
                self._detach()
                self.support, self.attach_rect = p, rect
                self.pos.y = p.y
                self.speed = 0.3 * self.walk_speed * h
                self.set_state(State.WALKING)

    # ---- dragging: the body hangs from the head like a pendulum
    def _update_dragged(self, dt: float) -> None:
        prev = self.anchor.copy()
        self.anchor = damp_vec(self.anchor, self.drag_target, 30.0, dt)
        if dt > 0:
            self.drag_vel = damp_vec(self.drag_vel, (self.anchor - prev) * (1.0 / dt), 14.0, dt)
        L = self.torso + self.head_r
        v = (self.hang - self.hang_prev) * max(0.0, 1.0 - 2.5 * dt)
        self.hang_prev = self.hang
        self.hang = self.hang + v + Vec(0, GRAVITY * self.h * dt * dt)
        d = self.hang - self.anchor
        n = d.length() or 1.0
        self.hang = self.anchor + d * (L / n)
        axis = (self.anchor - self.hang) * (1.0 / L)
        self.hang_angle = math.atan2(axis.x, -axis.y)

    def _pin_head_to_anchor(self) -> None:
        d = self.anchor - self.joints["head"]
        self.pos = self.pos + d
        for k, v in self.joints.items():
            self.joints[k] = v + d
        self.curves = {k: (a + d, c + d, b + d) for k, (a, c, b) in self.curves.items()}
        self.foot_targets = [t + d for t in self.foot_targets]

    # ----------------------------------------------------------------- pose
    def _shoulder_world(self) -> Vec:
        return self.joints.get("shoulder", self.pos + Vec(0, -0.7 * self.h))

    def _target_pose(self):
        """Returns hip(rel pos), lean, feet(rel pos), hands(rel shoulder), rates."""
        h, f, leg, t, st = self.h, self.facing, self.leg, self.time, self.state
        arm = self.upper_arm + self.forearm
        sq = max(self.squash, 0.0)
        tuck = self.stroke * 0.5  # contact points sit on top of edges
        default_rates = (22.0, 10.0, 32.0, 16.0)  # hip, lean, feet, hands

        if st == State.DRAGGED:
            th = self.hang_angle
            dv = self.drag_vel
            lag = Vec(clamp(-dv.x * 0.03, -0.3 * h, 0.3 * h), clamp(-dv.y * 0.03, -0.3 * h, 0.3 * h))
            fl = math.sin(t * 11.0)
            hip = Vec(0, -leg * 0.9)
            feet = [hip + rotate(Vec(-0.05 * h + 0.03 * h * fl, leg * 0.99), th) + lag,
                    hip + rotate(Vec(0.06 * h - 0.03 * h * fl, leg * 0.97), th) + lag * 1.25]
            hands = [rotate(Vec(-0.24 * h, -0.08 * h - 0.06 * h * fl), th) + lag * 0.8,
                     rotate(Vec(0.24 * h, -0.10 * h + 0.06 * h * fl), th) + lag * 0.8]
            return hip, th, feet, hands, (40.0, 40.0, 9.0, 11.0)

        if st in (State.FALLING, State.HOP) and self.jumping:
            # deliberate jump: arms up and knees tucked on the way up,
            # legs reaching down for the floor on the way down
            up = clamp(-self.vel.y / (JUMP_UP * h), 0.0, 1.0)
            down = clamp(self.vel.y / (JUMP_UP * h), 0.0, 1.0)
            tuck_k = max(0.0, 1.0 - down * 1.6)
            hip = Vec(0, -leg * 0.88)
            lean = f * (0.12 * up - 0.05 * down)
            feet = [Vec(f * 0.12 * h * tuck_k, -0.22 * h * tuck_k - 0.02 * h),
                    Vec(-f * 0.02 * h, -0.13 * h * tuck_k - 0.01 * h)]
            hands = [Vec(f * (0.06 + 0.12 * down) * h, -0.30 * h * (1 - down) + 0.05 * h * down),
                     Vec(f * (0.14 + 0.06 * down) * h, -0.27 * h * (1 - down) + 0.02 * h * down)]
            return hip, lean, feet, hands, (24.0, 12.0, 20.0, 18.0)

        if st == State.JUMP_PREP:  # anticipation: crouch, arms swing back
            k = smoothstep(self.state_time / JUMP_PREP_TIME)
            hip = Vec(-f * 0.02 * h, -leg * (0.95 - 0.28 * k) - tuck)
            feet = [Vec(-f * 0.06 * h, -tuck), Vec(f * 0.06 * h, -tuck)]
            hands = [Vec(-f * 0.22 * h, 0.15 * h), Vec(-f * 0.18 * h, 0.19 * h)]
            return hip, f * 0.38 * k, feet, hands, (30.0, 16.0, 32.0, 22.0)

        if st == State.SITTING:
            return self._sit_pose()

        if st == State.FALLING:
            flail = math.sin(t * 15.0)
            up = clamp(-self.vel.y / (h * 4.0), 0.0, 1.0)
            hip = Vec(0, -leg * 0.86)
            lean = -f * (0.10 - 0.15 * up)
            feet = [Vec(f * 0.14 * h, -0.11 * h + 0.03 * h * flail),
                    Vec(-f * 0.05 * h, -0.02 * h - 0.03 * h * flail)]
            hands = [Vec(f * 0.20 * h, -0.17 * h - 0.04 * h * flail),
                     Vec(-f * 0.23 * h, -0.12 * h + 0.04 * h * flail)]
            return hip, lean, feet, hands, (22.0, 8.0, 14.0, 14.0)

        if st in CLIMB_STATES and self.wall is not None:
            return self._climb_pose()

        r, run = self._gait_mix()
        rate = self._gait_rate()
        # duty factor: share of the cycle a foot is planted. 0.5 = walking;
        # below 0.5 both feet leave the ground for a moment = running.
        duty = 0.5 - 0.17 * run
        L = self.speed * duty / rate if rate > 0 else 0.0  # stance sweep, no foot sliding
        lift = 0.075 * h * min(1.0, r * 1.6) + 0.11 * h * run
        stance = 0.07 * h * (1.0 - r)
        feet = []
        for i in (0, 1):
            ph = (self.phase + 0.5 * i) % 1.0
            if ph < duty:  # stance: planted, moves back relative to the hip
                fx, fy = L / 2 - L * (ph / duty), 0.0
            else:          # swing: arc forward (a running foot kicks up behind first)
                s = (ph - duty) / (1.0 - duty)
                fx = -L / 2 + L * smoothstep(s) - 0.10 * h * run * math.sin(math.pi * min(1.0, s * 1.6))
                fy = -lift * math.sin(math.pi * s) ** (1.0 - 0.4 * run)
            feet.append(Vec(f * fx + (-1 if i == 0 else 1) * f * stance, fy - tuck))

        bob = 0.025 * h * r * (0.5 + 0.5 * math.cos(4 * math.pi * self.phase)) * (1 - run)
        bounce = 0.035 * h * run * math.cos(4 * math.pi * (self.phase - duty / 2))  # low mid-stance, high in flight
        breathe = 0.006 * h * math.sin(t * 2.4) * (1 - r)
        hip = Vec(f * (0.015 * r + 0.03 * run) * h,
                  -leg * (0.975 - 0.05 * r - 0.04 * run) + bob + bounce + breathe + sq * 0.30 * leg - tuck)
        lean = f * (0.09 * r + 0.20 * run + 0.32 * sq)
        if st == State.IDLE:
            lean += f * 0.04 * math.sin(t * 1.3)

        hands = []
        for i in (0, 1):
            ph = (self.phase + 0.5 * i) % 1.0
            swing = -math.cos(2 * math.pi * ph)  # opposite to the same-side leg
            # walking: loose arms swinging; running: elbows bent ~90deg, pumping
            hx = f * (swing * (0.13 * (1 - run) + 0.17 * run) * h * r + 0.015 * h + sq * 0.10 * h) \
                + (-1 if i == 0 else 1) * 0.035 * h * (1 - r)
            hy = (arm * 0.95 - 0.04 * h * max(0.0, swing) * r) * (1 - run) \
                + (0.16 * h - 0.07 * h * max(0.0, swing)) * run - sq * 0.10 * h
            hands.append(Vec(hx, hy))
        if self.gesture == "wave" and self.time < self.gesture_until:
            # near arm up above the head, hand swinging side to side
            hands[1] = Vec(f * 0.08 * h + 0.09 * h * math.sin(t * 11.0), -0.27 * h)
            lean -= f * 0.05
        elif self.gesture == "point" and self.time < self.gesture_until:
            # arm stretched toward what he's "shooting" at
            sh = self._shoulder_world()
            px, py = getattr(self, "point_at", (sh.x + f * 100, sh.y))
            dx, dy = px - sh.x, py - sh.y
            n = math.hypot(dx, dy) or 1.0
            hands[1] = Vec(dx / n * arm * 0.97, dy / n * arm * 0.97)
            self.facing = 1 if dx >= 0 else -1
        elif self.gesture == "type" and self.time < self.gesture_until:
            # bent over, hammering the field with both fists ("typing")
            k = math.sin(t * 24.0)
            hands = [Vec(f * 0.16 * h, arm * 0.62 + 0.07 * h * k), Vec(f * 0.21 * h, arm * 0.62 - 0.07 * h * k)]
            lean += f * 0.30
        return hip, lean, feet, hands, default_rates

    def _sit_pose(self):
        """Sitting: on the floor with knees up, or on a window's edge with the
        legs dangling over it, swinging lazily."""
        h, f, t, pos = self.h, self.facing, self.time, self.pos
        tuck = self.stroke * 0.5
        sh = self._shoulder_world()
        ground = pos.y - tuck
        rates = (7.0, 5.0, 9.0, 9.0)  # slow, heavy settle
        breathe = 0.004 * h * math.sin(t * 2.0)
        sp = self.support
        if self.sit_edge and sp is not None and not sp.is_floor:
            # on the edge with the legs hanging down over the front of the
            # window, swinging out of step, hands resting beside the hips
            s1, s2 = math.sin(t * 2.3), math.sin(t * 2.3 + 2.2)
            hip = Vec(0.0, -tuck - 0.02 * h + breathe)
            feet = [Vec(f * (0.09 * h + 0.06 * h * s1), 0.36 * h - 0.03 * h * max(0.0, s1)),
                    Vec(f * (0.13 * h + 0.06 * h * s2), 0.34 * h - 0.03 * h * max(0.0, s2))]
            hands = [Vec(pos.x - f * 0.14 * h, ground) - sh, Vec(pos.x + f * 0.10 * h, ground) - sh]
            return hip, f * 0.05 + 0.02 * math.sin(t * 0.7), feet, hands, rates
        hip = Vec(-f * 0.02 * h, -tuck - 0.02 * h + breathe)
        feet = [Vec(f * 0.27 * h, -tuck), Vec(f * 0.33 * h, -tuck)]
        hands = [Vec(pos.x - f * 0.16 * h, ground) - sh, Vec(pos.x - f * 0.09 * h, ground) - sh]
        return hip, -f * 0.16 + 0.02 * math.sin(t * 0.7), feet, hands, rates

    def _climb_pose(self):
        h, f, leg, st, w = self.h, self.facing, self.leg, self.state, self.wall
        tuck = self.stroke * 0.5
        sh = self._shoulder_world()
        pos = self.pos
        wx = w.x - f * tuck  # contact x just outside the window edge

        if st == State.CLIMB_PREP:  # crouch, arms swing back: anticipation
            k = smoothstep(self.state_time / 0.2)
            hip = Vec(-f * 0.03 * h, -leg * (0.92 - 0.2 * k))
            feet = [Vec(-f * 0.07 * h, -tuck), Vec(f * 0.07 * h, -tuck)]
            hands = [Vec(-f * 0.20 * h, 0.18 * h), Vec(-f * 0.16 * h, 0.22 * h)]
            return hip, f * 0.35 * k, feet, hands, (26.0, 14.0, 32.0, 18.0)

        if st == State.CLIMB_JUMP:  # reach for the wall, knees up
            u = clamp(self.state_time / self.jump_time, 0.0, 1.0)
            reach_y = self.jump_to.y - 0.62 * h
            hands = [Vec(wx, reach_y) - sh, Vec(wx, reach_y - 0.08 * h) - sh]
            hip = Vec(-f * 0.02 * h, -leg * (0.75 + 0.05 * u))
            feet = [Vec(f * 0.06 * h, -0.14 * h), Vec(f * 0.12 * h, -0.06 * h)]
            return hip, f * 0.12, feet, hands, (24.0, 14.0, 22.0, 20.0)

        if st == State.CLIMBING:  # hand over hand; planted limbs don't slide
            Lc = CLIMB_STRIDE * h
            hands, feet = [], []
            for i in (0, 1):
                ph = (self.climb_phase + 0.5 * i) % 1.0
                if ph < 0.5:
                    dy, out = -Lc / 2 + Lc * (ph / 0.5), 0.0
                else:
                    s = (ph - 0.5) / 0.5
                    dy, out = Lc / 2 - Lc * smoothstep(s), 0.07 * h * math.sin(math.pi * s)
                hands.append(Vec(wx - f * out - sh.x, -0.15 * h + dy))
                pf = (ph + 0.5) % 1.0  # feet move opposite to the same-side hand
                if pf < 0.5:
                    fdy, fout = -Lc / 2 + Lc * (pf / 0.5), 0.0
                else:
                    s = (pf - 0.5) / 0.5
                    fdy, fout = Lc / 2 - Lc * smoothstep(s), 0.06 * h * math.sin(math.pi * s)
                feet.append(Vec(wx - f * fout - pos.x, -0.09 * h + fdy))
            hip = Vec(-f * 0.03 * h, -leg * 0.72 + 0.01 * h * math.sin(4 * math.pi * self.climb_phase))
            return hip, f * 0.08, feet, hands, (18.0, 10.0, 30.0, 30.0)

        # MANTLE: hands planted on the top edge, push up, lead foot steps over
        u = clamp(self.state_time / MANTLE_TIME, 0.0, 1.0)
        top = w.y0 - tuck
        if u < 0.75:
            hands = [Vec(w.x + f * 0.03 * h, top) - sh, Vec(w.x + f * 0.12 * h, top) - sh]
        else:
            arm = self.upper_arm + self.forearm
            hands = [Vec(-f * 0.03 * h, arm * 0.95), Vec(f * 0.04 * h, arm * 0.95)]
        lead = Vec(w.x + f * 0.14 * h, top) if u > 0.35 else Vec(wx, pos.y - 0.30 * h)
        trail = Vec(w.x + f * 0.30 * h, top) if u > 0.7 else Vec(wx, pos.y - 0.12 * h)
        feet = [trail - pos, lead - pos]
        hip = Vec(-f * 0.04 * h * (1 - u), -leg * lerp(0.6, 0.9, smoothstep((u - 0.5) / 0.5)) - tuck)
        lean = f * 0.4 * (1.0 - smoothstep((u - 0.6) / 0.4))
        return hip, lean, feet, hands, (14.0, 9.0, 20.0, 26.0)

    def _update_pose(self, dt: float) -> None:
        hip, lean, feet, hands, (r_hip, r_lean, r_feet, r_hands) = self._target_pose()
        self.p_hip = damp_vec(self.p_hip, hip, r_hip, dt)
        if self.state == State.DRAGGED:
            self.p_lean = lean
            self.p_chest = damp(self.p_chest, lean, 25.0, dt)
        else:
            self.p_lean = damp(self.p_lean, lean, r_lean, dt)
            self.p_chest = damp(self.p_chest, self.p_lean, r_lean * 0.55, dt)
        self.p_head = damp(self.p_head, self.p_chest, 7.0, dt)
        self.p_facing = damp(self.p_facing, float(self.facing), 12.0, dt)
        self.p_feet = [damp_vec(c, t, r_feet, dt) for c, t in zip(self.p_feet, feet)]
        self.p_hands = [damp_vec(c, t, r_hands, dt) for c, t in zip(self.p_hands, hands)]

    def _build_joints(self) -> None:
        h, p = self.h, self.pos
        hip = p + self.p_hip
        d_pelvis, d_chest = up_dir(self.p_lean), up_dir(self.p_chest)
        neck = hip + normalize(d_pelvis + d_chest) * self.torso
        spine_ctrl = hip + d_pelvis * (self.torso * 0.55)
        head = neck + up_dir(self.p_head) * (self.head_r * 0.95)
        shoulder = quad_point(hip, spine_ctrl, neck, 0.86)
        j = {"hip": hip, "neck": neck, "head": head, "shoulder": shoulder}
        self.foot_targets = []
        for i in (0, 1):
            target = p + self.p_feet[i]
            self.foot_targets.append(target)
            j[f"knee{i}"], j[f"foot{i}"] = solve_two_bone(hip, target, self.thigh, self.shin, -self.p_facing)
            j[f"elbow{i}"], j[f"hand{i}"] = solve_two_bone(
                shoulder, shoulder + self.p_hands[i], self.upper_arm, self.forearm, self.p_facing)
        self.joints = j
        curves = {"spine": (hip, spine_ctrl, neck)}
        for name, a, m, b in LIMBS:
            curves[name] = (j[a], through_control(j[a], j[m], j[b]), j[b])
        self.curves = curves

    def bbox(self) -> Rect:
        pts = list(self.joints.values()) + [c for _, c, _ in self.curves.values()]
        m = self.head_r + self.stroke + 4
        return Rect(int(min(v.x for v in pts) - m), int(min(v.y for v in pts) - m),
                    int(max(v.x for v in pts) + m), int(max(v.y for v in pts) + m))
