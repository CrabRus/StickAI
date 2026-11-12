"""QPainter drawing for stickmen and the debug layer.

All coordinates are physical desktop pixels; each overlay sets up the
painter transform so this module never thinks about DPI.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

from .anim.mathutil import Vec
from .anim.stickman import Stickman
from .desktop.scene import DesktopScene


def _pt(v) -> QPointF:
    return QPointF(v.x, v.y)


def _polyline(p: QPainter, color: QColor, width: float, *pts) -> None:
    pen = QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath(_pt(pts[0]))
    for v in pts[1:]:
        path.lineTo(_pt(v))
    p.drawPath(path)


def _curve_path(curve) -> QPainterPath:
    a, c, b = curve
    path = QPainterPath(_pt(a))
    path.quadTo(_pt(c), _pt(b))
    return path


def _stroke(p: QPainter, color: QColor, width: float, path: QPainterPath) -> None:
    p.setPen(QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)


# Practically invisible (alpha 1/255) but non-zero: Windows hit-tests layered
# windows per pixel, so this halo is what makes the stickman easy to grab.
HIT_HALO = QColor(0, 0, 0, 1)


_ORDER = ("arm0", "leg0", "spine", "leg1", "arm1")


def paint_stickman(p: QPainter, s: Stickman) -> None:
    """All five curves go into ONE path and one drawPath call (the figure is a
    single flat colour, so overlap order doesn't matter); that's ~5x cheaper
    than stroking each limb separately."""
    j, cv = s.joints, s.curves
    color = QColor(s.color)
    body = QPainterPath()
    for k in _ORDER:
        a, c, b = cv[k]
        body.moveTo(a.x, a.y)
        body.quadTo(c.x, c.y, b.x, b.y)
    head = _pt(j["head"])

    if getattr(s, "show_halo", False):
        # nearly invisible fat outline so the cursor can grab him easily;
        # only drawn while the cursor is close (it is expensive to rasterize)
        halo = s.stroke + 2 * max(6.0, 0.05 * s.h)
        _stroke(p, HIT_HALO, halo, body)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(HIT_HALO)
        p.drawEllipse(head, s.head_r + halo / 2, s.head_r + halo / 2)

    if s.dust:
        paint_dust(p, s)
    _stroke(p, color, s.stroke, body)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawEllipse(head, s.head_r, s.head_r)
    prop = s.prop_active()
    if prop:
        paint_prop(p, s, prop)
    if getattr(s, "rod_on", False):
        paint_rod(p, s)


def paint_dust(p: QPainter, s: Stickman) -> None:
    """Soft grey puffs rolling out from the feet after a landing."""
    from .anim.stickman import DUST_TIME
    live = []
    p.setPen(Qt.PenStyle.NoPen)
    for x, y, t0, d, size in s.dust:
        k = (s.time - t0) / DUST_TIME
        if k >= 1.0 or k < 0:
            continue
        live.append((x, y, t0, d, size))
        ease = 1.0 - (1.0 - k) ** 2
        for i in range(3):
            r = s.h * size * (0.05 + 0.06 * ease) * (1.0 - 0.2 * i)
            cx = x + d * s.h * size * (0.12 + 0.18 * i + 0.30 * ease)
            cy = y - r * 0.8 - 0.02 * s.h * i
            c = QColor(200, 196, 188)
            c.setAlpha(int(150 * (1.0 - k) * (1.0 - 0.25 * i)))
            p.setBrush(c)
            p.drawEllipse(QPointF(cx, cy), r, r * 0.8)
    s.dust[:] = live


def paint_prop(p: QPainter, s: Stickman, prop: str) -> None:
    """The thing in their hands while they do a task."""
    h, f, t = s.h, s.facing, s.time
    j = s.joints
    h0, h1 = j["hand0"], j["hand1"]
    w = max(1.5, s.stroke * 0.55)
    p.save()
    if prop == "laptop":
        # side view: the base on the forearm, the lid up at the far end, the screen glowing toward him
        a = QPointF(h0.x - f * 0.04 * h, h0.y - 0.02 * h)
        b = QPointF(h0.x + f * 0.24 * h, h0.y - 0.02 * h)
        lid = QPointF(b.x() + f * 0.06 * h, b.y() - 0.22 * h)
        p.setPen(QPen(QColor(150, 154, 160), w * 1.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(a, b)
        p.setPen(QPen(QColor(185, 188, 194), w * 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(b, lid)
        glow = QColor(140, 210, 255)
        glow.setAlpha(150 + int(80 * (0.5 + 0.5 * math.sin(t * 7.0))))
        p.setPen(QPen(glow, w * 0.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(b.x() - f * 0.02 * h, b.y() - 0.04 * h), QPointF(lid.x() - f * 0.02 * h, lid.y() + 0.03 * h))
    elif prop == "magnifier":
        c = QPointF(h1.x + f * 0.07 * h, h1.y - 0.07 * h)
        p.setPen(QPen(QColor(110, 80, 50), w * 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(h1.x, h1.y), QPointF(c.x() - f * 0.035 * h, c.y() + 0.035 * h))
        glass = QColor(190, 230, 255, 120)
        p.setBrush(glass)
        p.setPen(QPen(QColor(90, 90, 100), w))
        p.drawEllipse(c, 0.065 * h, 0.065 * h)
        p.setPen(QPen(QColor(255, 255, 255, 200), w * 0.6))
        p.drawArc(QRectF(c.x() - 0.04 * h, c.y() - 0.04 * h, 0.08 * h, 0.08 * h), 100 * 16, 60 * 16)
    elif prop == "box":
        x0, x1 = min(h0.x, h1.x) - 0.02 * h, max(h0.x, h1.x) + 0.02 * h
        y1 = max(h0.y, h1.y) + 0.01 * h
        r = QRectF(x0, y1 - 0.20 * h, x1 - x0, 0.20 * h)
        p.setPen(QPen(QColor(140, 100, 60), w * 0.8))
        p.setBrush(QColor(200, 160, 110))
        p.drawRect(r)
        p.setPen(QPen(QColor(235, 215, 170), w * 0.9))
        p.drawLine(QPointF(r.center().x(), r.top()), QPointF(r.center().x(), r.top() + r.height() * 0.45))
    elif prop == "letter":
        r = QRectF(h1.x - 0.08 * h, h1.y - 0.13 * h, 0.17 * h, 0.11 * h)
        p.setPen(QPen(QColor(150, 150, 160), w * 0.6))
        p.setBrush(QColor(250, 250, 250))
        p.drawRect(r)
        p.drawLine(r.topLeft(), QPointF(r.center().x(), r.center().y() + 0.01 * h))
        p.drawLine(r.topRight(), QPointF(r.center().x(), r.center().y() + 0.01 * h))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(220, 60, 60))
        p.drawEllipse(QPointF(r.center().x(), r.center().y() + 0.012 * h), 0.012 * h, 0.012 * h)
    elif prop == "map":
        x0, x1 = min(h0.x, h1.x), max(h0.x, h1.x)
        top = min(h0.y, h1.y) - 0.15 * h
        r = QRectF(x0, top, max(0.1 * h, x1 - x0), 0.17 * h)
        p.setPen(QPen(QColor(170, 150, 110), w * 0.6))
        p.setBrush(QColor(240, 228, 190))
        p.drawRect(r)
        p.setPen(QPen(QColor(200, 185, 150), w * 0.5))
        for i in (1, 2):
            x = r.left() + r.width() * i / 3
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        p.setPen(QPen(QColor(90, 160, 220), w * 0.6))
        p.drawLine(QPointF(r.left() + r.width() * 0.15, r.bottom() - 0.04 * h), QPointF(r.right() - r.width() * 0.3, r.top() + 0.05 * h))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(230, 50, 50))
        p.drawEllipse(QPointF(r.right() - r.width() * 0.3, r.top() + 0.05 * h), 0.018 * h, 0.018 * h)
    elif prop == "remote":
        d = 0.10 * h
        a = QPointF(h1.x - f * 0.02 * h, h1.y + 0.01 * h)
        b = QPointF(h1.x + f * d, h1.y - 0.01 * h)
        p.setPen(QPen(QColor(45, 45, 50), w * 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(a, b)
        if math.sin(t * 9.0) > 0.3:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 70, 70))
            p.drawEllipse(b, 0.012 * h, 0.012 * h)
    p.restore()


ROD_COLOR, LINE_COLOR, REEL_COLOR = QColor(122, 82, 48), QColor(235, 235, 235, 230), QColor(150, 150, 160)


def paint_rod(p: QPainter, s: Stickman) -> None:
    """Fishing rod from the hands, the line to the hook (sagging until it's pulled taut)."""
    from .anim.mathutil import normalize
    from .anim.stickman import ROD_LEN
    h = s.h
    grip = s.rod_grip if s.rod_grip.length() > 0 else s.joints["hand1"]
    d = normalize(s.hook - grip)
    if d.length() < 0.5:
        d = Vec(s.facing, -0.6)
    tip = grip + d * (ROD_LEN * h)
    butt = grip - d * (0.10 * h)
    rod = QPainterPath()
    rod.moveTo(butt.x, butt.y)
    rod.lineTo(tip.x, tip.y)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(ROD_COLOR, max(1.5, s.stroke * 0.42), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawPath(rod)
    reel = grip + d * (0.05 * h) + Vec(-d.y, d.x) * (0.03 * h * s.facing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(REEL_COLOR)
    p.drawEllipse(_pt(reel), 0.03 * h, 0.03 * h)
    line = QPainterPath()
    line.moveTo(tip.x, tip.y)
    hk = s.hook
    if s.hook_taut:
        line.lineTo(hk.x, hk.y)
    else:  # slack: sags under the middle
        sag = 0.08 * (hk - tip).length()
        line.quadTo((tip.x + hk.x) / 2, (tip.y + hk.y) / 2 + sag, hk.x, hk.y)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(LINE_COLOR, max(1.0, s.stroke * 0.14)))
    p.drawPath(line)
    # the hook: a small grey J
    r = 0.035 * h
    hook = QPainterPath()
    hook.moveTo(hk.x, hk.y - 2 * r)
    hook.lineTo(hk.x, hk.y)
    hook.quadTo(hk.x, hk.y + r * 1.4, hk.x - r * 1.2, hk.y + 0.2 * r)
    p.setPen(QPen(QColor(170, 170, 180), max(1.2, s.stroke * 0.22), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawPath(hook)


def _cosmetic(color: QColor, width: float, style=Qt.PenStyle.SolidLine) -> QPen:
    pen = QPen(color, width, style)
    pen.setCosmetic(True)  # width in device pixels regardless of transform
    return pen


def paint_debug(p: QPainter, scene: DesktopScene, stickmen: list[Stickman], ui_scale: float) -> None:
    p.setBrush(Qt.BrushStyle.NoBrush)

    # every window we consider solid
    p.setPen(_cosmetic(QColor(160, 160, 160, 90), 1))
    for w in scene.windows:
        r = w.rect
        p.drawRect(QRectF(r.left, r.top, r.width, r.height))

    # walkable platforms
    for pl in scene.platforms:
        color = (QColor(0, 200, 255, 200) if pl.is_floor else
                 QColor(*{"heading": (255, 120, 220, 230), "image": (190, 120, 255, 230),
                         "button": (255, 150, 60, 230)}.get(pl.kind, (255, 210, 40, 230))) if pl.widget
                 else QColor(60, 230, 90, 220))
        p.setPen(_cosmetic(color, 3 * ui_scale))
        p.drawLine(QPointF(pl.x0, pl.y), QPointF(pl.x1, pl.y))

    # climbable side edges (orange) vs. edges that can't be mantled (grey)
    for wl in scene.walls:
        color = QColor(255, 120, 40, 220) if wl.climbable else QColor(150, 150, 150, 120)
        p.setPen(_cosmetic(color, (3 if wl.climbable else 1) * ui_scale))
        p.drawLine(QPointF(wl.x, wl.y0), QPointF(wl.x, wl.y1))

    # the active (foreground) window
    fg = scene.foreground
    if fg is not None:
        r = fg.rect
        p.setPen(_cosmetic(QColor(255, 170, 0, 230), 2 * ui_scale, Qt.PenStyle.DashLine))
        p.drawRect(QRectF(r.left, r.top, r.width, r.height))
        font = QFont("Segoe UI")
        font.setPixelSize(max(9, round(12 * ui_scale)))
        p.setFont(font)
        label = f"ACTIVE  {fg.title[:60]}  [{r.left},{r.top} → {r.right},{r.bottom}]  {r.width}×{r.height}"
        p.setPen(QColor(255, 170, 0))
        p.drawText(QPointF(r.left + 6 * ui_scale, r.top + 16 * ui_scale), label)

    # what each stickman is standing on / climbing
    for s in stickmen:
        p.setPen(_cosmetic(QColor(255, 0, 200, 230), 4 * ui_scale))
        if s.support is not None:
            p.drawLine(QPointF(s.support.x0, s.support.y), QPointF(s.support.x1, s.support.y))
        if s.wall is not None:
            p.drawLine(QPointF(s.wall.x, s.wall.y0), QPointF(s.wall.x, s.wall.y1))


def paint_stickman_debug(p: QPainter, s: Stickman) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 0, 200, 200))
    for t in s.foot_targets:
        p.drawEllipse(_pt(t), 2.5, 2.5)


def paint_hud(p: QPainter, x: float, y: float, ui_scale: float, lines: list[str]) -> None:
    font = QFont("Consolas")
    font.setPixelSize(max(9, round(12 * ui_scale)))
    p.setFont(font)
    lh = 16 * ui_scale
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(0, 0, 0, 150))
    p.drawRoundedRect(QRectF(x, y, 520 * ui_scale, (len(lines) * 16 + 10) * ui_scale), 6 * ui_scale, 6 * ui_scale)
    p.setPen(QColor(230, 230, 230))
    for i, line in enumerate(lines):
        p.drawText(QPointF(x + 8 * ui_scale, y + (i + 1) * lh), line)
