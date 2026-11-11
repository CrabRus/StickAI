"""Things a stickman may do on your PC, always after you press "Allow".

Supported tasks (all coming from replies to *your* chat message, never from
their own banter, and never more than one at a time):
  close_window     leap onto the window, glide the cursor to its X, stomp, close
  minimize_window  same, on the _ button
  restore_window   open a minimized / background window and bring it forward
  type_text        hop onto the field, glide + click into it, type the text
  click_button     find a button / link / tab by its name (UI Automation), glide there, click
  scroll_page      scroll a window up / down; with "read", look at it again and summarise
  arrange_windows  snap windows into halves / quarters / thirds of a screen, or maximize
  launch_app       open a program from the allow-list in Settings
  check_field      spell-check another app's text field ON THIS PC, then offer the fixes
  set_clipboard    put a result (translation, fix...) on the clipboard      (instant)
  remind / cancel_reminders   local timers, see reminders.py                 (instant)

Each task is a little script (a generator) stepped by Qt timers, so the
animation and the app keep running while it plays. Grabbing the mouse
while the cursor is gliding aborts the task.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import os
import threading
from datetime import datetime

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QGuiApplication
from ..ui.dialogs import StickDialog, ask_async

from ..anim.mathutil import smoothstep
from ..anim.stickman import State
from ..desktop import win32
from . import aim, control, uia
from .files import Workspace, WorkspaceError
from ..i18n import tr

if TYPE_CHECKING:
    from ..app import StickApp

log = logging.getLogger("stickai.tasks")

# "...without asking" for windows covers these:
WINDOW_KINDS = ("close_window", "minimize_window", "restore_window", "arrange_windows", "scroll_page", "launch_app")
FILE_KINDS = ("create_folder", "write_file", "open_path")
INSTANT_KINDS = ("set_clipboard", "remind", "cancel_reminders")  # harmless, done at once, never block
ASKS_IN_SCRIPT = ("check_field",)  # reads first, then asks with the actual fixes
KINDS = WINDOW_KINDS + ("type_text", "click_game", "click_button") + ASKS_IN_SCRIPT + FILE_KINDS + INSTANT_KINDS
MAX_CLIPBOARD = 20000
MAX_TYPOS_SHOWN = 12
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
        self._answer = False  # result of the last ("ask", ...) step
        self._chat_aside = False  # the chat was hidden because it covered the task's target

    # ------------------------------------------------------------ proposal
    def propose(self, key: str, task: dict, targets: dict) -> None:
        name = self.app.director.name(key)
        if str(task.get("kind", "")) in INSTANT_KINDS:
            spec = self._resolve(task, targets)
            if spec is not None:
                self._instant(key, spec)
            return
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
                  (settings.trust_files and spec["kind"] in FILE_KINDS) or spec["kind"] in ASKS_IN_SCRIPT
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
        if kind in INSTANT_KINDS or kind == "launch_app":
            return self._resolve_other(kind, task)
        if not settings.control:
            return None
        if kind in ("click_button", "scroll_page", "arrange_windows", "check_field"):
            return self._resolve_window_tool(kind, task, targets)
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

    def _resolve_other(self, kind: str, task: dict) -> dict | None:
        """Clipboard, reminders and the app launcher (each has its own setting)."""
        settings = self.app.roster.settings
        text = str(task.get("text", "")).strip()
        if kind == "set_clipboard":
            if not settings.clipboard or not text:
                return None
            return {"kind": kind, "text": text[:MAX_CLIPBOARD], "what": tr("task.clip")}
        if kind in ("remind", "cancel_reminders"):
            if not settings.reminders:
                return None
            seconds = task.get("seconds")
            if kind == "remind" and not (isinstance(seconds, (int, float)) and text):
                return None
            return {"kind": kind, "text": text, "seconds": float(seconds or 0), "what": ""}
        # launch_app: only names from your allow-list, matched loosely ("spotify" -> "Spotify")
        want = str(task.get("app", "") or text).strip().lower()
        for name, path in settings.apps:
            if want and (want == name.lower() or want in name.lower()):
                return {"kind": kind, "app": name, "path": path, "what": tr("task.launch", app=name)}
        return None

    def _resolve_window_tool(self, kind: str, task: dict, targets: dict) -> dict | None:
        settings = self.app.roster.settings
        wins, titles = targets.get("windows", {}), targets.get("titles", {})

        def window(v):
            return wins.get(int(v)) if isinstance(v, (int, float)) else None

        if kind == "arrange_windows":
            moves = []
            for m in (task.get("arrange") or [])[:6]:
                if not isinstance(m, dict):
                    continue
                hwnd, place = window(m.get("window")), str(m.get("place", ""))
                if hwnd is not None and place in control.PLACES:
                    moves.append((hwnd, place, int(m["window"])))
            if not moves and window(task.get("window")) is not None and str(task.get("place", "")) in control.PLACES:
                moves = [(window(task["window"]), str(task["place"]), int(task["window"]))]
            if not moves:
                return None
            mon = task.get("monitor")
            what = ", ".join(f"“{titles.get(i, '')[:30]}” → {tr('place.' + p)}" for _h, p, i in moves)
            return {"kind": kind, "moves": [(h, p) for h, p, _i in moves],
                    "monitor": int(mon) if isinstance(mon, (int, float)) else None,
                    "what": tr("task.arrange", what=what)}
        if kind == "check_field":
            if not settings.read_fields:
                return None
            fid = task.get("field")
            field = targets.get("fields", {}).get(int(fid)) if isinstance(fid, (int, float)) else None
            if field is None:
                return None
            hwnd, rel, fname = field
            return {"kind": kind, "hwnd": hwnd, "rel": rel, "field": fname or "…",
                    "what": tr("task.spell", field=fname or "…")}
        hwnd = window(task.get("window"))
        if hwnd is None:
            return None
        title = titles.get(int(task["window"]), "")[:50]
        if kind == "click_button":
            button = " ".join(str(task.get("text", "")).split())[:60]
            if not button:
                return None
            return {"kind": kind, "hwnd": hwnd, "button": button, "title": title,
                    "what": tr("task.click", button=button, title=title)}
        # scroll_page
        direction = "up" if str(task.get("direction", "down")).lower() == "up" else "down"
        amount = task.get("amount")
        amount = min(max(float(amount), 0.25), 5.0) if isinstance(amount, (int, float)) else 1.0
        read = bool(task.get("read")) and settings.vision != "off"
        key = "task.scroll_read" if read else "task.scroll"
        return {"kind": kind, "hwnd": hwnd, "direction": direction, "amount": amount, "read": read,
                "what": tr(key, dir=tr("dir." + direction), title=title)}

    def _instant(self, key: str, spec: dict) -> None:
        """Clipboard and reminders: nothing to animate or approve, done right away."""
        app, name, kind = self.app, self.app.director.name(key), spec["kind"]
        if kind == "set_clipboard":
            QGuiApplication.clipboard().setText(spec["text"])
            app.director.note_event(tr("task.done", name=name, what=spec["what"]))
            s = app.world.get(key)
            if s is not None:
                s.jump()
        elif kind == "remind":
            r = app.reminders.add(key, spec["seconds"], spec["text"])
            if r is not None:
                app.director.note_event(tr("rem.set", name=name, time=f"{datetime.fromtimestamp(r.due):%H:%M}",
                                           text=r.text))
        elif kind == "cancel_reminders":
            app.director.note_event(tr("rem.cancelled", n=app.reminders.cancel(spec["text"])))

    @staticmethod
    def _background(fn):
        """Run fn on a thread (UI Automation can take a moment); poll box["done"]."""
        box: dict = {}

        def run():
            try:
                box["value"] = fn()
            except Exception as e:  # noqa: BLE001
                log.info("background step failed: %s", e)
                box["value"] = None
            box["done"] = True
        threading.Thread(target=run, daemon=True).start()
        return box

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
            self._chat_back()
            return
        if cmd[0] == "wait":
            QTimer.singleShot(int(cmd[1] * 1000), self._step)
        elif cmd[0] == "until":
            _, cond, timeout = cmd
            self._poll(cond, timeout)
        elif cmd[0] == "ask":  # ("ask", title, text, accept label): Allow / Deny in the middle of a task
            _, title, text, accept = cmd
            self.app.chat.show()

            def answered(ok: bool) -> None:
                self._box, self._answer = None, ok
                if self._chat_aside:  # it came back only to hold the question: aside again
                    self.app.chat.hide()
                self._step()
            self._box = ask_async(self.app.chat, title, text, accept, tr("perm.deny"), answered)
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
        self._chat_back()

    def _aside(self, x: float, y: float) -> bool:
        """If one of OUR windows (the always-on-top chat...) covers (x, y), step it aside for
        the task. True if the point is now free for the target window."""
        root = control.root_at(x, y)
        if root and control.window_pid(root) == os.getpid() and self.app.chat.isVisible():
            self.app.chat.hide()
            self._chat_aside = True
            return True
        return False

    def _chat_back(self) -> None:
        if self._chat_aside:
            self._chat_aside = False
            if not self.app.hidden:
                self.app.chat.show()

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

            elif kind == "launch_app":
                s.jump()
                yield ("wait", 0.35)
                try:
                    os.startfile(os.path.expandvars(spec["path"]))
                except OSError as e:
                    raise TaskFailed(str(e))

            elif kind == "arrange_windows":
                s.wave(1.2)
                mons = app.scene.monitors
                for hwnd, place in spec["moves"]:
                    if not control.exists(hwnd):
                        continue
                    control.restore_window(hwnd)
                    yield ("wait", 0.15)
                    r = win32.query_live_rect(hwnd)
                    mon = None
                    if spec["monitor"] is not None and 1 <= spec["monitor"] <= len(mons):
                        mon = mons[spec["monitor"] - 1]
                    elif r is not None:
                        cx, cy = (r.left + r.right) / 2, (r.top + r.bottom) / 2
                        mon = next((m for m in mons if m.bounds.contains(cx, cy)), None)
                    mon = mon or next((m for m in mons if m.primary), mons[0] if mons else None)
                    if mon is None or not control.place_window(hwnd, place, mon.work):
                        raise TaskFailed(tr("task.why_gone"))
                    yield ("wait", 0.2)

            elif kind == "click_button":
                control.restore_window(hwnd)
                yield ("wait", 0.3)
                box = self._background(lambda: uia.find_clickables(hwnd, spec["button"]))
                yield ("until", lambda: box.get("done", False), 6.0)
                hits = box.get("value") or []
                if not hits:
                    raise TaskFailed(tr("task.why_no_button", button=spec["button"]))
                left, top, right, bottom, label = hits[0]
                bx, by = (left + right) / 2, (top + bottom) / 2
                what = tr("task.click", button=label or spec["button"], title=spec["title"])
                rect = win32.query_live_rect(hwnd)
                if rect is not None:
                    plat = app.scene.platform_under(bx, rect.top, hwnd, window_tolerance=3)
                    if plat is not None and not plat.widget and s.leap_to(plat, min(max(bx, plat.x0 + 4), plat.x1 - 4)):
                        yield ("until", lambda: s.state not in (State.JUMP_PREP, State.HOP), 6.0)
                s.gesture, s.gesture_until, s.point_at = "point", s.time + 1.2, (bx, by)
                if self._aside(bx, by):
                    yield ("wait", 0.15)
                yield ("glide", bx, by, 0.6)
                if control.root_at(bx, by) != hwnd:
                    raise TaskFailed(tr("task.why_covered"))
                s.jump()
                yield ("wait", 0.25)
                control.click()

            elif kind == "scroll_page":
                control.restore_window(hwnd)
                yield ("wait", 0.3)
                rect = win32.query_live_rect(hwnd)
                if rect is None:
                    raise TaskFailed(tr("task.why_gone"))
                from ..desktop import widgets
                box = self._background(lambda: widgets.content_rect(hwnd))
                yield ("until", lambda: box.get("done", False), 3.0)
                doc = box.get("value")
                if doc is not None and doc[2] > doc[0] and doc[3] > doc[1]:
                    px, py = (doc[0] + doc[2]) / 2, (doc[1] + doc[3]) / 2
                else:
                    px, py = (rect.left + rect.right) / 2, rect.top + rect.height * 0.55
                s.gesture, s.gesture_until, s.point_at = "point", s.time + 2.0, (px, py)
                if self._aside(px, py):
                    yield ("wait", 0.15)
                yield ("glide", px, py, 0.5)
                if control.root_at(px, py) != hwnd:
                    raise TaskFailed(tr("task.why_covered"))
                notches = max(1, round(spec["amount"] * 8)) * (1 if spec["direction"] == "down" else -1)
                step = 1 if notches > 0 else -1
                for _ in range(abs(notches)):  # notch by notch: smooth, and it looks like scrolling
                    control.scroll(step)
                    yield ("wait", 0.04)
                yield ("wait", 0.6)  # let the page settle before anyone looks at it
                if spec["read"]:
                    app.director.note_event(tr("task.done", name=name, what=what))
                    app.director.read_after_scroll(key)
                    return

            elif kind == "check_field":
                rect = win32.query_live_rect(hwnd)
                if rect is None:
                    raise TaskFailed(tr("task.why_gone"))
                checker = app.chat.input.spell.checker
                if not checker.available:
                    raise TaskFailed(tr("spell.why_none"))
                control.restore_window(hwnd)
                yield ("wait", 0.3)
                rect = win32.query_live_rect(hwnd) or rect
                dx, dy, w, h = spec["rel"][:4]
                fx, fy = rect.left + dx + w / 2, rect.top + dy + h / 2
                plat = app.scene.platform_under(fx, rect.top + dy, hwnd, window_tolerance=4)
                if plat is not None and s.leap_to(plat, fx):
                    yield ("until", lambda: s.state not in (State.JUMP_PREP, State.HOP), 6.0)
                if self._aside(fx, fy):
                    yield ("wait", 0.15)
                if control.root_at(fx, fy) != hwnd:
                    raise TaskFailed(tr("task.why_covered"))
                box = self._background(lambda: uia.read_field(fx, fy))
                yield ("until", lambda: box.get("done", False), 4.0)
                text, problem = box.get("value") or (None, "unreadable")
                if text is None:
                    raise TaskFailed(tr("spell.why_password" if problem == "password" else "spell.why_unreadable"))
                from ..ui.spellcheck import WORD
                fixes: dict[str, str] = {}
                for m in WORD.finditer(text):
                    word = m.group()
                    if word not in fixes and not checker.is_ok(word):
                        sugg = checker.suggest(word)
                        if sugg:
                            fixes[word] = sugg[0]
                if not fixes:
                    app.say(key, tr("spell.clean"), "jump", force_bubble=True)
                    what = tr("task.spell", field=spec["field"])
                else:
                    shown = "\n".join(f"{a}  →  {b}" for a, b in list(fixes.items())[:MAX_TYPOS_SHOWN])
                    if len(fixes) > MAX_TYPOS_SHOWN:
                        shown += "\n…"
                    yield ("ask", tr("perm.title"),
                           f"{name} " + tr("spell.found", n=len(fixes), field=spec["field"], list=shown),
                           tr("spell.fix"))
                    if not self._answer:
                        app.director.note_event(tr("task.denied", name=name, what=what))
                        return
                    fixed = WORD.sub(lambda m: fixes.get(m.group(), m.group()), text)
                    s.gesture, s.gesture_until = "type", s.time + 1.2
                    box = self._background(lambda: uia.write_field(fx, fy, fixed))
                    yield ("until", lambda: box.get("done", False), 4.0)
                    s.gesture = ""
                    if not box.get("value"):
                        raise TaskFailed(tr("spell.why_write"))
                    s.jump()
                    what = tr("spell.fixed", n=len(fixes), field=spec["field"])

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
                if self._aside(fx, fy):
                    yield ("wait", 0.15)
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
