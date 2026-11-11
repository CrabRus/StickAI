"""Things a stickman may do on your PC, always after you press "Allow".

Supported tasks (all coming from replies to *your* chat message, never from
their own banter, and never more than one at a time):
  close_window     leap onto the window, glide the cursor to its X, stomp, close
  minimize_window  same, on the _ button
  restore_window   open a minimized / background window and bring it forward
  type_text        hop onto the field, glide + click into it, type the text

Each task is a little script (a generator) stepped by Qt timers, so the
animation and the app keep running while it plays. Grabbing the mouse
while the cursor is gliding aborts the task.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer
from ..ui.dialogs import StickDialog, ask_async

from ..anim.mathutil import smoothstep
from ..anim.stickman import State
from ..desktop import aim, control, win32
from ..desktop.files import Workspace, WorkspaceError
from ..i18n import tr

if TYPE_CHECKING:
    from ..app import StickApp

log = logging.getLogger("stickai.tasks")

WINDOW_KINDS = ("close_window", "minimize_window", "restore_window")
FILE_KINDS = ("create_folder", "write_file", "open_path")
KINDS = WINDOW_KINDS + ("type_text", "click_game") + FILE_KINDS
MAX_TEXT = 300
GAME_MAX_SECONDS = 300


class TaskFailed(Exception):
    pass


class TaskRunner(QObject):
    def __init__(self, app: "StickApp"):
        super().__init__()
        self.app = app
        self.busy = False
        self._gen = None
        self._box: StickDialog | None = None
        self._glide = None  # (x0, y0, x1, y1, t, duration, last_set)
        self._glide_timer = QTimer(interval=15)
        self._glide_timer.timeout.connect(self._glide_tick)
        self.game_running = False

    # ------------------------------------------------------------ proposal
    def propose(self, key: str, task: dict, targets: dict) -> None:
        name = self.app.director.name(key)
        if self.busy or self._box is not None:
            self.app.director.note_event(tr("task.busy", name=name))
            return
        spec = self._resolve(task, targets)
        if spec is None:
            log.info("ignored unusable task from %s: %s", key, task)
            return
        what = spec["what"]
        settings = self.app.roster.settings
        trusted = (settings.trust_windows and spec["kind"] in WINDOW_KINDS) or \
                  (settings.trust_files and spec["kind"] in FILE_KINDS)
        if trusted:  # you switched "without asking" on for this kind of task
            self._start(key, spec)
            return
        if self.app.hidden:
            self.app.toggle_hidden()
        self.app.chat.show()

        def decided(ok: bool) -> None:  # Enter = Allow (highlighted); Esc / ✕ = Deny
            self._box = None
            if not ok:
                self.app.director.note_event(tr("task.denied", name=name, what=what))
                return
            self._start(key, spec)

        self._box = ask_async(self.app.chat, tr("perm.title"), tr("perm.question", name=name, what=what),
                              tr("perm.allow"), tr("perm.deny"), decided)

    def _resolve(self, task: dict, targets: dict) -> dict | None:
        kind = str(task.get("kind", ""))
        if kind not in KINDS:
            return None
        settings = self.app.roster.settings
        if kind in FILE_KINDS:
            if not settings.files:
                return None
            ws = Workspace(settings.files_folder)
            path = str(task.get("path", "")).strip()
            try:
                target = ws.root if kind == "open_path" and path in ("", ".", "/") else ws.resolve(path)
            except WorkspaceError:
                return None
            shown = target.relative_to(ws.root).as_posix() if target != ws.root else ws.root.name
            if kind == "create_folder":
                what = tr("task.folder", path=shown)
            elif kind == "write_file":
                what = tr("task.write_edit" if target.exists() else "task.write_new", path=shown)
                preview = str(task.get("content", ""))[:160].replace("\n", " ⏎ ")
                what += f":\n\n{preview}{'…' if len(str(task.get('content', ''))) > 160 else ''}"
            else:
                what = tr("task.open", path=shown)
            return {"kind": kind, "path": path, "content": str(task.get("content", "")), "what": what}
        if not settings.control:
            return None
        if kind == "click_game":
            wid = task.get("window")
            hwnd = targets.get("windows", {}).get(int(wid)) if isinstance(wid, (int, float)) else None
            color = str(task.get("color", "")).strip().lstrip("#")
            rgb = tuple(int(color[i:i + 2], 16) for i in (0, 2, 4)) if len(color) == 6 and \
                all(c in "0123456789abcdefABCDEF" for c in color) else None
            at = (task.get("at_x"), task.get("at_y"))
            at = tuple(float(v) for v in at) if all(isinstance(v, (int, float)) for v in at) else None
            if hwnd is None:
                return None  # (no colour given is fine: the detector guesses it)
            title = targets.get("titles", {}).get(int(wid), "")[:60]
            return {"kind": kind, "hwnd": hwnd, "color": rgb, "at": at, "what": tr("task.game", title=title)}
        if kind == "type_text":
            fid = task.get("field")
            field = targets.get("fields", {}).get(int(fid)) if isinstance(fid, (int, float)) else None
            text = " ".join(str(task.get("text", "")).split())[:MAX_TEXT]  # one line, no Enter
            if field is None or not text:
                return None
            hwnd, rel, fname = field
            title = targets.get("titles", {}).get(targets.get("window_of", {}).get(hwnd), "") or ""
            return {"kind": kind, "hwnd": hwnd, "rel": rel, "text": text,
                    "what": tr("task.type", field=fname or "…", title=title[:50], text=text)}
        wid = task.get("window")
        hwnd = targets.get("windows", {}).get(int(wid)) if isinstance(wid, (int, float)) else None
        if hwnd is None:
            return None
        title = targets.get("titles", {}).get(int(wid), "")[:60]
        key = {"close_window": "task.close", "minimize_window": "task.minimize", "restore_window": "task.restore"}[kind]
        return {"kind": kind, "hwnd": hwnd, "what": tr(key, title=title)}

    def stop_game(self) -> bool:
        """'stop' in the chat: end a running game. True if one was running."""
        if not self.game_running:
            return False
        self.game_running = False
        return True

    # ------------------------------------------------------------- running
    def _start(self, key: str, spec: dict) -> None:
        self.busy = True
        self._gen = self._script(key, spec)
        self._step()

    def _step(self, *_a) -> None:
        if self._gen is None:
            return
        try:
            cmd = next(self._gen)
        except StopIteration:
            self._gen = None
            self.busy = False
            return
        if cmd[0] == "wait":
            QTimer.singleShot(int(cmd[1] * 1000), self._step)
        elif cmd[0] == "until":
            _, cond, timeout = cmd
            self._poll(cond, timeout)
        elif cmd[0] == "glide":
            _, x, y, dur = cmd
            mons = self.app.scene.monitors
            if mons and not any(m.bounds.contains(x, y) for m in mons):  # off-screen: clamp to the nearest screen
                m = min(mons, key=lambda m: abs((m.bounds.left + m.bounds.right) / 2 - x) + abs((m.bounds.top + m.bounds.bottom) / 2 - y))
                x = min(max(x, m.bounds.left + 1), m.bounds.right - 2)
                y = min(max(y, m.bounds.top + 1), m.bounds.bottom - 2)
            cx, cy = win32.cursor_pos()
            self._glide = [cx, cy, x, y, 0.0, dur, (cx, cy)]
            self._glide_timer.start()

    def _poll(self, cond, timeout: float) -> None:
        if cond() or timeout <= 0:
            self._step()
        else:
            QTimer.singleShot(80, lambda: self._poll(cond, timeout - 0.08))

    def _glide_tick(self) -> None:
        g = self._glide
        if g is None:
            self._glide_timer.stop()
            return
        cur = win32.cursor_pos()
        # g[6] is where the cursor really ended up after our last move (Windows may clamp or
        # nudge it), so only movement made by YOU since then counts as taking the mouse back
        if abs(cur[0] - g[6][0]) > 40 or abs(cur[1] - g[6][1]) > 40:
            self._glide_timer.stop()
            self._glide = None
            self._fail(tr("task.why_mouse"))
            return
        g[4] += 0.015
        u = smoothstep(min(1.0, g[4] / g[5]))
        x, y = g[0] + (g[2] - g[0]) * u, g[1] + (g[3] - g[1]) * u
        control.set_cursor(x, y)
        g[6] = win32.cursor_pos()
        if u >= 1.0:
            self._glide_timer.stop()
            self._glide = None
            self._step()

    def _fail(self, why: str) -> None:
        if self._gen is not None:
            try:
                self._gen.throw(TaskFailed(why))
            except (StopIteration, TaskFailed):
                pass
        self._gen = None
        self.busy = False

    # ------------------------------------------------------------- scripts
    def _script(self, key: str, spec: dict):
        app, name, what = self.app, self.app.director.name(key), spec["what"]
        s = app.world.get(key)
        try:
            if s is None:
                raise TaskFailed("…")
            hwnd = spec.get("hwnd")  # file tasks have no window
            if hwnd is not None and not control.exists(hwnd):
                raise TaskFailed(tr("task.why_gone"))
            kind = spec["kind"]

            if kind == "restore_window":
                s.wave(1.5)
                yield ("wait", 0.6)
                control.restore_window(hwnd)

            elif kind in ("close_window", "minimize_window"):
                rect = win32.query_live_rect(hwnd)
                if rect is None:
                    raise TaskFailed(tr("task.why_gone"))
                bx, by = control.caption_buttons(hwnd, rect)["close" if kind == "close_window" else "minimize"]
                top = app.scene.platform_under(bx, rect.top, hwnd, window_tolerance=3)
                if top is not None and not top.widget and s.leap_to(top, min(max(bx, top.x0 + 4), top.x1 - 4)):
                    yield ("until", lambda: s.state not in (State.JUMP_PREP, State.HOP), 6.0)
                yield ("glide", bx, by, 0.6)
                s.jump()  # the stomp on the button
                yield ("wait", 0.4)
                if not control.exists(hwnd):
                    raise TaskFailed(tr("task.why_gone"))
                (control.close_window if kind == "close_window" else control.minimize_window)(hwnd)

            elif kind in FILE_KINDS:
                ws = Workspace(app.roster.settings.files_folder)
                try:
                    if kind == "create_folder":
                        ws.create_folder(spec["path"])
                    elif kind == "write_file":
                        ws.write_file(spec["path"], spec["content"])
                    else:
                        ws.open("" if spec["path"] in ("", ".", "/") else spec["path"])
                except (WorkspaceError, OSError) as e:
                    raise TaskFailed(str(e))
                s.jump()  # ta-da
                what = what.split(":\n\n")[0]  # log without the content preview

            elif kind == "click_game":
                yield from self._game(s, spec)
                return

            elif kind == "type_text":
                rect = win32.query_live_rect(hwnd)
                if rect is None:
                    raise TaskFailed(tr("task.why_gone"))
                control.restore_window(hwnd)  # in front, so the click lands on it
                yield ("wait", 0.25)
                rect = win32.query_live_rect(hwnd) or rect
                dx, dy, w, h = spec["rel"][:4]
                fx, fy = rect.left + dx + min(w / 2, 60), rect.top + dy + h / 2  # inside the field, near its start
                plat = app.scene.platform_under(fx, rect.top + dy, hwnd, window_tolerance=4)
                if plat is not None and s.leap_to(plat, fx):
                    yield ("until", lambda: s.state not in (State.JUMP_PREP, State.HOP), 6.0)
                yield ("glide", fx, fy, 0.6)
                if control.root_at(fx, fy) != hwnd:  # something else is in front of the field
                    raise TaskFailed(tr("task.why_focus"))
                control.click()
                yield ("wait", 0.3)
                if control.foreground_root() != hwnd:
                    raise TaskFailed(tr("task.why_focus"))
                s.gesture, s.gesture_until = "type", s.time + 0.05 * len(spec["text"]) + 1.0
                for ch in spec["text"]:
                    if control.foreground_root() != hwnd:  # you switched away: stop typing
                        raise TaskFailed(tr("task.why_focus"))
                    control.type_char(ch)
                    yield ("wait", 0.045)
                s.gesture = ""
            app.director.note_event(tr("task.done", name=name, what=what))
        except TaskFailed as e:
            if s is not None:
                s.gesture = ""
            app.director.note_event(tr("task.failed", name=name, what=what, why=str(e)))

    def _game(self, s, spec):
        """Click targets in the game until stopped (chat 'stop', moving the mouse,
        the window closing, or GAME_MAX_SECONDS).

        Only the page / game area is used (in a browser: below the tabs, address
        bar and extension icons), and anything that stays put after 3 clicks in a
        row (a button or icon, not a target) is ignored for the rest of the game.
        """
        import time
        from ..desktop import widgets
        from ..desktop.win32 import Rect
        app, hwnd = self.app, spec["hwnd"]
        name = app.director.name(s.key)
        clicks = wasted = 0
        self.game_running = True
        try:
            rect = win32.query_live_rect(hwnd)
            if rect is None:
                raise TaskFailed(tr("task.why_gone"))
            # target colour: sample the exact pixel colour where Gemini saw a target
            color = spec["color"]
            if spec["at"] is not None:
                full = app.grab_screen()
                sampled = aim.sample_color(aim.to_array(full), *spec["at"])
                if color is None or sum((a - b) ** 2 for a, b in zip(sampled, color)) < 120 ** 2:
                    color = sampled
            top = app.scene.platform_under((rect.left + rect.right) / 2, rect.top, hwnd, window_tolerance=3)
            if top is not None and not top.widget and s.leap_to(top, (rect.left + rect.right) / 2):
                yield ("until", lambda: s.state not in (State.JUMP_PREP, State.HOP), 6.0)
            log.debug("game: leap done")
            chat_was_open = app.chat.isVisible()
            app.chat.hide()  # out of the way: it would cover the game (Alt+` brings it back)
            log.debug("game: chat hidden")
            control.restore_window(hwnd)  # the game window gets the clicks
            log.debug("game: game window in front")
            yield ("wait", 0.3)
            dpi = (control.user32.GetDpiForWindow(hwnd) or 96) / 96.0

            # ---- the play area, relative to the window so it follows it around
            rect = win32.query_live_rect(hwnd) or rect
            info = app.scene.window(hwnd)
            browser = info is not None and info.class_name in widgets.BROWSER_CLASSES
            # "where is the page?" can take ~0.5 s in a big browser window: ask on a thread
            import threading
            box = {}
            threading.Thread(target=lambda: box.update(doc=widgets.content_rect(hwnd), done=True), daemon=True).start()
            yield ("until", lambda: box.get("done", False), 3.0)
            doc = box.get("doc")
            min_top = (110 if browser else 36) * dpi  # tabs + toolbar / title bar
            if doc is not None and (doc[2] - doc[0]) * (doc[3] - doc[1]) > 0.2 * rect.width * rect.height:
                rel = (max(doc[0] - rect.left, 0), max(doc[1] - rect.top, min_top if doc[1] <= rect.top + 4 else 0),
                       min(doc[2] - rect.left, rect.width), min(doc[3] - rect.top, rect.height))
            else:
                rel = (4, min_top, rect.width - 4, rect.height - 4)
            log.info("game: play area %s inside window %s (page found: %s)", rel, rect, doc is not None)

            def area_now():
                r = win32.query_live_rect(hwnd)
                return None if r is None else (int(r.left + rel[0]), int(r.top + rel[1]),
                                               int(r.left + rel[2]), int(r.top + rel[3]))

            blacklist: list[tuple[float, float, float]] = []  # (dx, dy, radius) from the window corner

            def blacklist_now():
                r = win32.query_live_rect(hwnd)
                return [] if r is None else [(r.left + dx, r.top + dy, rad) for dx, dy, rad in blacklist]

            # capture + detection run on a background thread; this loop only aims and clicks
            log.info("game: target colour %s", color or "(guessing)")
            finder = aim.TargetFinder(hwnd, color, min_size=int(10 * dpi))
            own_now = lambda: [(o.left, o.top, o.right, o.bottom) for o in app.own_rects()]
            finder.update(area_now(), [], own_now())
            finder.start()
            last, repeat, last_click_t = None, 0, 0.0
            end = time.monotonic() + GAME_MAX_SECONDS
            waited = 0.0
            while self.game_running and time.monotonic() < end:
                area = area_now()
                if area is None:
                    break
                finder.update(area, blacklist_now(), own_now())
                t, found = finder.latest()
                if t <= last_click_t + 0.05:  # wait for a picture taken after the last click
                    waited += 0.02
                    yield ("wait", 0.02)
                    continue
                if not found:
                    waited += 0.03
                    if waited > 8 and finder.color is None:
                        raise TaskFailed("no targets visible")
                    last_click_t = t  # nothing in that frame: look at the next one
                    yield ("wait", 0.03)
                    continue
                waited = 0.0
                if color is None and finder.color is not None:
                    color = finder.color
                    log.info("game: target colour %s (guessed)", color)
                cx, cy = win32.cursor_pos()
                px, py, size = min(found, key=lambda b: (b[0] - cx) ** 2 + (b[1] - cy) ** 2)
                # the same spot again and again? then it isn't a target (a button, an icon...)
                if last is not None and (px - last[0]) ** 2 + (py - last[1]) ** 2 < max(14 * dpi, size / 3) ** 2:
                    repeat += 1
                else:
                    repeat = 0
                if repeat >= 3:
                    r = win32.query_live_rect(hwnd)
                    if r is not None:
                        blacklist.append((px - r.left, py - r.top, size / 2 + 8 * dpi))
                    wasted += 3
                    log.info("game: ignoring a spot that doesn't react at (%d, %d)", px, py)
                    last, repeat, last_click_t = None, 0, time.monotonic()
                    continue
                s.gesture, s.gesture_until = "point", s.time + 0.6
                s.point_at = (px, py)
                yield ("glide", px, py, 0.09)
                if control.root_at(px, py) != hwnd:  # something moved in front meanwhile: don't click it
                    continue
                control.click()
                clicks += 1
                last, last_click_t = (px, py), time.monotonic()
                yield ("wait", 0.08)
        except TaskFailed as e:
            if str(e) != tr("task.why_mouse"):  # moving the mouse is the normal way to stop
                app.director.note_event(tr("task.failed", name=name, what=spec["what"], why=str(e)))
        finally:
            self.game_running = False
            if locals().get("finder") is not None:
                finder.stop()
            s.gesture = ""
            if locals().get("chat_was_open") and not app.hidden:
                app.chat.show()
        app.director.note_event(tr("task.game_over", name=name, n=max(0, clicks - wasted)))
        s.jump()
