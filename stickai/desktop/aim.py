"""Fast local target finder for click / aim games.

Gemini looks at ONE screenshot and tells us what a target looks like (its
colour, and roughly where one is so we can sample the exact colour). From
then on this module finds targets on its own, several times a second:

  1. take the game window's pixels (QImage from a screen grab),
  2. keep pixels close to the target colour (RGB distance),
  3. group them into blobs on a coarse grid (connected cells),
  4. return blob centres, biggest first.

Pixels covered by our own stickmen / bubbles are masked out, so a red
stickman never mistakes himself for a red target.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtGui import QImage

STEP = 3          # sample every 3rd pixel (4K window -> fast)
CELL = 4          # grid cell size in sampled pixels for blob grouping


def to_array(img: QImage) -> np.ndarray:
    """QImage -> HxWx3 uint8 RGB array (copy)."""
    img = img.convertToFormat(QImage.Format.Format_RGB32)
    h, w, bpl = img.height(), img.width(), img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=h * bpl).reshape(h, bpl)
    bgra = buf[:, : w * 4].reshape(h, w, 4)
    return bgra[:, :, 2::-1].copy()  # BGRA -> RGB


def sample_color(rgb: np.ndarray, fx: float, fy: float, radius: int = 6) -> tuple[int, int, int]:
    """Median colour around a point given as fractions (0..1) of the image."""
    h, w = rgb.shape[:2]
    x, y = int(min(max(fx, 0.0), 1.0) * (w - 1)), int(min(max(fy, 0.0), 1.0) * (h - 1))
    patch = rgb[max(0, y - radius): y + radius + 1, max(0, x - radius): x + radius + 1].reshape(-1, 3)
    return tuple(int(c) for c in np.median(patch, axis=0))


def find_targets(rgb: np.ndarray, color: tuple[int, int, int], tolerance: float = 60.0,
                 min_size: int = 6, exclude: list[tuple[int, int, int, int]] = ()) -> list[tuple[int, int, int]]:
    """Blobs of `color` -> [(x, y, size), ...] in image pixels, biggest first.

    exclude: rectangles (x0, y0, x1, y1) in image pixels to ignore.
    min_size: minimum blob width/height in image pixels.
    """
    small = rgb[::STEP, ::STEP].astype(np.int32)  # int32: squared differences overflow int16
    diff = small - np.array(color, dtype=np.int32)
    mask = (diff * diff).sum(axis=2) <= tolerance * tolerance
    for x0, y0, x1, y1 in exclude:
        mask[max(0, y0 // STEP): max(0, y1 // STEP + 1), max(0, x0 // STEP): max(0, x1 // STEP + 1)] = False
    if not mask.any():
        return []
    # coarse grid: a cell is "on" if enough of its pixels match
    h, w = mask.shape
    gh, gw = h // CELL, w // CELL
    if gh == 0 or gw == 0:
        return []
    grid = mask[: gh * CELL, : gw * CELL].reshape(gh, CELL, gw, CELL).mean(axis=(1, 3)) > 0.3
    # connected components (4-neighbour flood fill over the small grid)
    seen = np.zeros_like(grid, dtype=bool)
    blobs = []
    ys, xs = np.nonzero(grid)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if seen[sy, sx]:
            continue
        stack, cells = [(sy, sx)], []
        seen[sy, sx] = True
        while stack:
            cy, cx = stack.pop()
            cells.append((cy, cx))
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < gh and 0 <= nx < gw and grid[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        cy = sum(c[0] for c in cells) / len(cells)
        cx = sum(c[1] for c in cells) / len(cells)
        span = (max(c[0] for c in cells) - min(c[0] for c in cells) + 1,
                max(c[1] for c in cells) - min(c[1] for c in cells) + 1)
        size_px = max(span) * CELL * STEP
        if size_px >= min_size and len(cells) <= gh * gw * 0.4:  # not the whole background
            blobs.append((int((cx + 0.5) * CELL * STEP), int((cy + 0.5) * CELL * STEP), size_px))
    blobs.sort(key=lambda b: -b[2])
    return blobs


def guess_target_color(rgb: np.ndarray, exclude: list[tuple[int, int, int, int]] = (),
                       min_size: int = 12) -> tuple[int, int, int] | None:
    """No colour given: find the vivid colour that looks most like *targets*.

    Candidates are the most common vivid colours; each is scored by how many
    separate, target-sized blobs it forms (not tiny specks, not big banners or
    a whole background). Returns None if nothing target-like is visible.
    exclude: rectangles (image px) to ignore, e.g. our own stickmen.
    """
    masked = rgb.copy()
    for x0, y0, x1, y1 in exclude:
        masked[max(0, y0): max(0, y1), max(0, x0): max(0, x1)] = 128  # neutral grey = not vivid
    small = masked[::4, ::4].reshape(-1, 3).astype(np.int32)
    hi, lo = small.max(axis=1), small.min(axis=1)
    vivid = small[(hi - lo > 90) & (hi > 120)]
    if len(vivid) < 20:
        return None
    bins = vivid // 32
    keys = bins[:, 0] * 64 + bins[:, 1] * 8 + bins[:, 2]
    counts = np.bincount(keys)
    max_size = 0.3 * min(rgb.shape[:2])
    best, best_score = None, 0.0
    for key in np.argsort(counts)[::-1][:6]:
        if counts[key] < 5:
            break
        color = tuple(int(c) for c in vivid[keys == key].mean(axis=0))
        blobs = find_targets(masked, color, tolerance=50, min_size=min_size)
        good = [b for b in blobs if b[2] <= max_size]
        if len(good) < len(blobs):  # part of a big banner / background: unlikely to be targets
            continue
        score = len(good) + 0.001 * counts[key]
        if good and score > best_score:
            best, best_score = color, score
    return best


class TargetFinder:
    """Background thread: capture the play area, find targets, repeat.

    The UI thread only sets what to look for (update()) and reads the latest
    result (latest()), so a slow 4K capture never freezes the animation.
    Results are in physical screen pixels: [(x, y, size), ...].
    """

    def __init__(self, hwnd: int, color, min_size: int):
        import threading
        self.hwnd, self.color, self.min_size = hwnd, color, min_size
        self._area = None          # (left, top, right, bottom) physical px
        self._blacklist = []       # [(x, y, radius)] physical px
        self._own = []             # [(l, t, r, b)] our stickmen / bubbles / windows (physical px)
        self._result: tuple[float, list] = (0.0, [])
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="aim-finder", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def update(self, area, blacklist, own=()) -> None:
        with self._lock:
            self._area, self._blacklist, self._own = area, list(blacklist), list(own)

    def latest(self) -> tuple[float, list]:
        """(time.monotonic() of the capture, targets)."""
        with self._lock:
            return self._result

    def _run(self) -> None:
        import time
        from . import capture, control
        while not self._stop.is_set():
            with self._lock:
                area, blacklist, own = self._area, self._blacklist, self._own
            if area is None:
                self._stop.wait(0.03)
                continue
            left, top, right, bottom = area
            w, h = right - left, bottom - top
            scale = 0.5 if w * h > 1_500_000 else 1.0
            t = time.monotonic()
            rgb = capture.grab(left, top, w, h, scale)
            if rgb is None:
                self._stop.wait(0.1)
                continue
            exclude = [(int((x - r - left) * scale), int((y - r - top) * scale),
                        int((x + r - left) * scale), int((y + r - top) * scale)) for x, y, r in blacklist]
            # the screen copy includes our own see-through windows: never aim at ourselves
            exclude += [(int((l - left) * scale), int((t - top) * scale), int((r - left) * scale),
                         int((b - top) * scale)) for l, t, r, b in own]
            if self.color is None:
                self.color = guess_target_color(rgb, exclude, min_size=max(4, int(self.min_size * scale)))
                if self.color is None:
                    with self._lock:
                        self._result = (t, [])
                    self._stop.wait(0.2)
                    continue
            found = find_targets(rgb, self.color, min_size=max(4, int(self.min_size * scale)), exclude=exclude)
            out = []
            for bx, by, size in found:
                x, y = left + bx / scale, top + by / scale
                if control.root_at(x, y) == self.hwnd:  # a click there would really hit the game
                    out.append((x, y, size / scale))
            with self._lock:
                self._result = (t, out)
            self._stop.wait(max(0.0, 0.05 - (time.monotonic() - t)))
