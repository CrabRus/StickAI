"""Simulation container: the desktop scene plus every stickman in it."""
from __future__ import annotations

import math
import random
import time

from .anim.stickman import Stickman
from .desktop.scene import DesktopScene

MAX_SUBSTEP = 1.0 / 240.0  # physics never steps more than ~4 ms at a time


class World:
    def __init__(self, scene: DesktopScene):
        self.scene = scene
        self.stickmen: list[Stickman] = []

    def spawn_point(self, h: float) -> tuple[float, float]:
        """Somewhere along the top of the primary monitor, dropping in."""
        mons = self.scene.monitors
        m = next((m for m in mons if m.primary), mons[0] if mons else None)
        if m is None:
            return 200.0, 0.0
        wa = m.work
        return random.uniform(wa.left + 0.1 * wa.width, wa.right - 0.1 * wa.width), wa.top - 0.2 * h

    def get(self, key: str) -> Stickman | None:
        return next((s for s in self.stickmen if s.key == key and not s.leaving), None)

    def spawn(self, key: str, name: str, color: str, height: float) -> Stickman:
        x, y = self.spawn_point(height)
        s = Stickman(name, color, height, x, y, facing=random.choice((-1, 1)))
        s.key = key
        s.born = time.monotonic()
        self.stickmen.append(s)
        return s

    def step(self, dt: float) -> list[Stickman]:
        """Advance physics; returns stickmen that left the world this frame."""
        n = max(1, math.ceil(dt / MAX_SUBSTEP))
        sub = dt / n
        kill_y = self.scene.lowest_floor
        top = self.scene.highest_top
        for i in range(n):
            last = i == n - 1
            for s in self.stickmen:
                s.update(sub, self.scene, pose_dt=dt if last else None, first=i == 0)
        gone = []
        for s in list(self.stickmen):
            if s.leaving and s.pos.y < top - 1.5 * s.h:
                self.stickmen.remove(s)
                gone.append(s)
            elif s.pos.y > kill_y + 4 * s.h:  # fell out of the world somehow
                s.respawn(*self.spawn_point(s.h))
        return gone

    def pick(self, x: float, y: float, slack: float = 0.0) -> Stickman | None:
        """Topmost stickman under the point (last drawn = on top)."""
        for s in reversed(self.stickmen):
            if s.hit_test(x, y, slack * s.h):
                return s
        return None

    def on_scene_refreshed(self) -> None:
        for s in self.stickmen:
            s.resync_support(self.scene)

    def consume_support_changes(self) -> bool:
        changed = any(s.support_changed for s in self.stickmen)
        for s in self.stickmen:
            s.support_changed = False
        return changed
