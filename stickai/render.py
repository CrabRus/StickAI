"""QPainter drawing for stickmen and the debug layer.

All coordinates are physical desktop pixels; each overlay sets up the
painter transform so this module never thinks about DPI.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

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

    _stroke(p, color, s.stroke, body)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawEllipse(head, s.head_r, s.head_r)


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
