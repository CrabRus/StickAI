"""Build StickAI.exe (a small launcher without a console window) in the project folder.

    .venv\\Scripts\\python launcher\\build.py

Uses the C# compiler that ships with Windows (.NET Framework 4), so nothing extra is needed.
The icon is drawn with Qt: the yellow stickman from the tray.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CSC = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"


def make_icon(path: Path) -> None:
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen

    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841 - Qt needs one for painting
    img = QImage(256, 256, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(4, 4)  # drawn on the tray icon's 64 px grid
    color = QColor("#FEC503")
    outline = QPen(QColor(40, 40, 40, 200), 11, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    pen = QPen(color, 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    lines = [((32, 26), (32, 44)), ((32, 44), (20, 60)), ((32, 44), (44, 60)),
             ((32, 30), (18, 40)), ((32, 30), (46, 38))]
    for pn in (outline, pen):  # a dark rim so it shows on light and dark taskbars
        p.setPen(pn)
        for a, b in lines:
            p.drawLine(QPointF(*a), QPointF(*b))
    p.setPen(QPen(QColor(40, 40, 40, 200), 2))
    p.setBrush(color)
    p.drawEllipse(QRectF(22, 2, 20, 20))
    p.end()
    if not img.save(str(path), "ICO"):
        raise SystemExit("Qt could not write the .ico file")


def main() -> int:
    if not CSC.exists():
        print(f"C# compiler not found at {CSC}")
        return 1
    icon = HERE / "stickai.ico"
    make_icon(icon)
    out = ROOT / "StickAI.exe"
    cmd = [str(CSC), "/nologo", "/target:winexe", "/optimize+", f"/win32icon:{icon}",
           "/r:System.Windows.Forms.dll", "/r:System.Core.dll", f"/out:{out}", str(HERE / "StickAI.cs")]
    code = subprocess.call(cmd)
    if code == 0:
        print(f"built {out}")
    return code


if __name__ == "__main__":
    sys.exit(main())
