"""Small 2D math kit: vectors, frame-rate-independent smoothing, 2-bone IK."""
from __future__ import annotations

import math


class Vec:
    __slots__ = ("x", "y")

    def __init__(self, x: float = 0.0, y: float = 0.0):
        self.x = float(x)
        self.y = float(y)

    def __add__(self, o: "Vec") -> "Vec":
        return Vec(self.x + o.x, self.y + o.y)

    def __sub__(self, o: "Vec") -> "Vec":
        return Vec(self.x - o.x, self.y - o.y)

    def __mul__(self, k: float) -> "Vec":
        return Vec(self.x * k, self.y * k)

    __rmul__ = __mul__

    def length(self) -> float:
        return math.hypot(self.x, self.y)

    def copy(self) -> "Vec":
        return Vec(self.x, self.y)

    def __repr__(self) -> str:
        return f"Vec({self.x:.1f}, {self.y:.1f})"


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def ease_out_cubic(t: float) -> float:
    t = clamp(t, 0.0, 1.0)
    return 1.0 - (1.0 - t) ** 3


def rotate(v: Vec, angle: float) -> Vec:
    c, s = math.cos(angle), math.sin(angle)
    return Vec(v.x * c - v.y * s, v.x * s + v.y * c)


def normalize(v: Vec) -> Vec:
    n = v.length()
    return Vec(0.0, -1.0) if n < 1e-9 else Vec(v.x / n, v.y / n)


def up_dir(angle: float) -> Vec:
    """Unit vector pointing 'up' tilted by angle (screen coords, +angle leans right)."""
    return Vec(math.sin(angle), -math.cos(angle))


def quad_point(a: Vec, c: Vec, b: Vec, t: float) -> Vec:
    u = 1.0 - t
    return Vec(u * u * a.x + 2 * u * t * c.x + t * t * b.x, u * u * a.y + 2 * u * t * c.y + t * t * b.y)


def through_control(a: Vec, j: Vec, b: Vec, bulge: float = 1.4) -> Vec:
    """Control point of a quadratic Bezier from a to b shaped by joint j.

    bulge=2 makes the curve pass exactly through j; smaller values round the
    corner off more softly (1.0 = halfway). This is what turns an IK bone
    chain into a smooth rubber-hose limb.
    """
    mx, my = (a.x + b.x) * 0.5, (a.y + b.y) * 0.5
    return Vec(mx + (j.x - mx) * bulge, my + (j.y - my) * bulge)


def dist_to_segment(px: float, py: float, a: Vec, b: Vec) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 < 1e-9 else clamp(((px - a.x) * dx + (py - a.y) * dy) / L2, 0.0, 1.0)
    return math.hypot(px - (a.x + dx * t), py - (a.y + dy * t))


def smoothstep(t: float) -> float:
    t = clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def damp(current: float, target: float, rate: float, dt: float) -> float:
    """Exponential approach that behaves identically at 30, 60 or 240 FPS."""
    return target + (current - target) * math.exp(-rate * dt)


def damp_vec(current: Vec, target: Vec, rate: float, dt: float) -> Vec:
    a = math.exp(-rate * dt)
    return Vec(target.x + (current.x - target.x) * a, target.y + (current.y - target.y) * a)


def solve_two_bone(root: Vec, target: Vec, l1: float, l2: float, bend: float) -> tuple[Vec, Vec]:
    """Analytic two-bone IK (hip-knee-foot / shoulder-elbow-hand).

    `bend` in [-1, 1] picks which side the middle joint pops out to, relative
    to the root->target direction. Passing a *continuous* value lets the knee
    swing smoothly through straight when a character turns around.
    Returns (middle_joint, end_effector); the end is clamped to reach.
    """
    dx, dy = target.x - root.x, target.y - root.y
    d = math.hypot(dx, dy)
    if d < 1e-6:
        dx, dy, d = 0.0, 1.0, 1.0
    ux, uy = dx / d, dy / d
    reach = clamp(d, abs(l1 - l2) + 1e-3, l1 + l2 - 1e-3)
    a = (l1 * l1 - l2 * l2 + reach * reach) / (2.0 * reach)
    h = math.sqrt(max(0.0, l1 * l1 - a * a))
    px, py = -uy, ux
    joint = Vec(root.x + ux * a + px * h * bend, root.y + uy * a + py * h * bend)
    end = Vec(root.x + ux * reach, root.y + uy * reach)
    return joint, end
