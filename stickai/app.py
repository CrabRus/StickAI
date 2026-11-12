"""Application wiring: Qt app, overlays, timers, tray icon."""
from __future__ import annotations

import argparse
import logging
import os
import random
import shutil
import signal
import sys
import time

from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import config
from .brain.characters import Roster, load_roster
from .brain.director import Director
from .tools.tasks import TaskRunner
from .tools.reminders import ReminderBook
from .coding.helper import CodeHelper
from .tools.startapps import StartApps
from .desktop import win32
from .desktop.scene import DesktopScene
from .desktop.win32 import Rect
from .overlay import HUD_LOGICAL, OverlayWindow
from .ui.bubble import SpeechBubble, reading_time
from .brain.memory import MemoryStore
from .ui.chatwindow import ChatWindow
from .ui.quickask import QuickAsk
from .ui.toast import Toasts
from .ui.hotkey import GlobalHotkey
from .ui.settings import SettingsDialog
from .ui import theme
from .ui.dialogs import confirm
from .i18n import set_language, tr
from .world import World
from .activities.music import MusicShow
from .activities.couch import COUCH, Couch
from .activities.fight import Fight
from .desktop.widgets import WidgetScanner

log = logging.getLogger("stickai")
PROJECT_DIR = Path(__file__).resolve().parent.parent


def _tray_icon(color: str) -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    for a, b in [((32, 26), (32, 44)), ((32, 44), (20, 60)), ((32, 44), (44, 60)),
                 ((32, 30), (18, 40)), ((32, 30), (46, 38))]:
        p.drawLine(QPointF(*a), QPointF(*b))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(22, 2, 20, 20))
    p.end()
    return QIcon(pm)


def o_contains(app, screen, x, y) -> bool:
    """Does `screen` contain the physical point (x, y)? (uses the overlay on it)"""
    for o in app.overlays:
        if o.qscreen is screen and o.phys.left <= x < o.phys.right and o.phys.top <= y <= o.phys.bottom + 200:
            return True
    return False


class StickApp:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.qapp = QApplication(sys.argv)
        self.qapp.setQuitOnLastWindowClosed(False)
        if not win32.is_per_monitor_v2():
            log.warning("Process is not Per-Monitor-v2 DPI aware; window coordinates may be scaled.")
        win32.set_timer_resolution(True)
        self.qapp.aboutToQuit.connect(lambda: win32.set_timer_resolution(False))

        self.debug = args.debug and not args.no_debug  # outlines + HUD cost frame time: off by default
        self.paused = False
        self.hidden = False          # everything hidden by the hide hotkey
        self._restore_chat = False
        self._restore_settings = False
        self.scene = DesktopScene(exclude_pid=os.getpid(), headroom=40)
        self.world = World(self.scene)
        self.overlays: list[OverlayWindow] = []
        self.hud_overlay: OverlayWindow | None = None
        self.dragging = None  # Stickman being held by the mouse
        self.last_capture_bounds = None  # monitor shown in the last screenshot
        self._fps = 0.0
        self._last = time.perf_counter()
        self._ticks = 0

        self.bubbles: dict[str, SpeechBubble] = {}  # character key -> bubble
        self.settings_dialog: SettingsDialog | None = None

        # character sheets (hot-reloaded on save)
        self.characters_path = Path(args.characters).resolve()
        if not self.characters_path.exists():  # first run: start from the shipped defaults
            from .brain.characters import DEFAULTS_PATH
            shutil.copyfile(DEFAULTS_PATH, self.characters_path)
            log.info("Created %s from the defaults", self.characters_path)
        try:
            self.roster: Roster = load_roster(self.characters_path)
        except ValueError as e:
            raise SystemExit(f"Can't load characters: {e}")
        set_language(self.roster.settings.language)
        self._watcher = QFileSystemWatcher([str(self.characters_path)])
        self._watcher.fileChanged.connect(lambda _p: QTimer.singleShot(250, self._reload_roster))

        self.build_overlays()
        self.scene.refresh()

        # talking: chat window + Gemini director + persistent memory
        self.memory = MemoryStore(self.characters_path.parent / "memory")
        self.director = Director(self, self.characters_path.parent / "secrets.toml", self.memory)
        self.tasks = TaskRunner(self)
        self.code = CodeHelper(self, self.characters_path.parent / "secrets.toml")  # VS Code link
        self.reminders = ReminderBook(self, self.characters_path.parent / "memory" / "reminders.json")
        self.start_apps = StartApps()  # the Start menu's apps, read in the background
        self.music = MusicShow(self)
        self.couch = Couch(self)
        self.fight = Fight(self)
        self.chat = ChatWindow(self)
        self.chat.apply_on_top(self.roster.settings.chat_on_top)
        self._apply_spellcheck()
        for e in self.memory.entries[-80:]:  # older history stays on disk
            self.chat.add_entry(e, follow=False)
        self.chat.set_present([])
        self.chat.submitted.connect(lambda text, img: self.director.handle(text, img, to=self.chat.addressees()))
        self.chat.stop_requested.connect(self.director.cancel_thinking)
        self.chat.summon_all.connect(lambda: [self.summon(k) for k in self.roster.characters])
        self.director.thinking.connect(self.chat.set_thinking)
        self.toasts = Toasts(on_click=self.chat.summon)
        self.director.message.connect(self._maybe_toast)
        self.director.message.connect(self.chat.add_entry)
        self.director.status.connect(self.chat.set_status)
        self.chat.settings_requested.connect(self.open_settings)
        self.chat.quit_requested.connect(self.confirm_quit)
        self.chat.look_requested.connect(self.attach_screenshot)
        self.director.thinking.connect(lambda on: self.chat.set_status(tr("chat.thinking")) if on else
                                       (self.chat.status.text() == tr("chat.thinking") and self.chat.set_status("")))
        log.info("Memory: %d chat lines, %d long-term facts", len(self.memory.entries),
                 len(self.memory.facts.get("shared", [])))

        for key in args.spawn if args.spawn is not None else self.roster.settings.start_with:
            self.summon(key)

        for scr in QGuiApplication.screens():
            self._watch_screen(scr)
        self.qapp.screenAdded.connect(lambda s: (self._watch_screen(s), self._schedule_rebuild()))
        self.qapp.screenRemoved.connect(lambda _s: self._schedule_rebuild())

        # Frame loop: aim for the fastest connected display's refresh rate.
        hz = max((s.refreshRate() for s in QGuiApplication.screens()), default=60.0) or 60.0
        self.frame_timer = QTimer(timerType=Qt.TimerType.PreciseTimer)
        self.frame_timer.setInterval(max(1, int(1000.0 / hz)))
        self.frame_timer.timeout.connect(self._tick)
        self.frame_timer.start()
        log.info("Frame timer %d ms (display %.0f Hz)", self.frame_timer.interval(), hz)

        # OS polling loop.
        self.scene_timer = QTimer(interval=config.SCENE_POLL_MS)
        self.scene_timer.timeout.connect(self._poll_scene)
        self.scene_timer.start()

        self._build_tray()

        self.chat.setWindowIcon(self.tray.icon())
        self.hotkey = GlobalHotkey(self.toggle_chat)
        self.qapp.installNativeEventFilter(self.hotkey)
        self.hide_hotkey = GlobalHotkey(self.toggle_hidden, hotkey_id=0x5A2)
        self.qapp.installNativeEventFilter(self.hide_hotkey)
        self._register_hotkey()
        self.qapp.aboutToQuit.connect(self._shutdown)
        if not self.director.client.has_key():
            log.warning("No Gemini API key found: set GEMINI_API_KEY or create %s",
                        self.characters_path.parent / "secrets.toml")

        # widgets inside windows (search bars...) found in the background
        self.widget_scanner = WidgetScanner()
        self.widget_scanner.found.connect(self._on_widgets)
        self.widget_scanner.start()
        self.widget_scanner.set_enabled(self.roster.settings.widgets, self.roster.settings.web_surfaces)

        # characters chatting among themselves now and then
        self.banter_timer = QTimer(singleShot=True)
        self.banter_timer.timeout.connect(self._banter)
        self._schedule_banter()
        QTimer.singleShot(8000, self.director.maybe_summarize)  # catch up on last session's chat

        if args.exit_after:
            QTimer.singleShot(int(args.exit_after * 1000), self._exit_with_report)
        if args.trace:
            self._trace_timer = QTimer(interval=500)
            self._trace_timer.timeout.connect(self._trace)
            self._trace_timer.start()

    # ------------------------------------------------------------ overlays
    def _watch_screen(self, scr) -> None:
        scr.availableGeometryChanged.connect(lambda _g: self._schedule_rebuild())

    def _schedule_rebuild(self) -> None:
        QTimer.singleShot(300, self.build_overlays)

    def build_overlays(self) -> None:
        for o in self.overlays:
            o.close()
            o.deleteLater()
        self.overlays = []
        primary = QGuiApplication.primaryScreen()
        for scr in QGuiApplication.screens():
            o = OverlayWindow(scr, self)
            o.place()
            self.overlays.append(o)
            if scr is primary:
                self.hud_overlay = o
            log.info("Overlay %-14s logical=%s phys=%s scale=%.2f click-through=%s",
                     scr.name(), scr.availableGeometry().getRect(), o.phys, o.scale, o.click_through)
        if self.hud_overlay is None and self.overlays:
            self.hud_overlay = self.overlays[0]
        if getattr(self, "hidden", False):
            for o in self.overlays:
                o.hide()

    # ---------------------------------------------------------------- loops
    def _tick(self) -> None:
        now = time.perf_counter()
        dt = min(now - self._last, 0.1)  # don't explode after a stall
        self._last = now
        if dt > 0:
            self._fps = self._fps * 0.95 + (1.0 / dt) * 0.05 if self._fps else 1.0 / dt
        if self.hidden:
            return
        self._update_mouse()
        if self.paused:
            return
        before = {id(s): s.bbox() for s in self.world.stickmen}
        gone = self.world.step(dt)
        dirty = [before.pop(id(s)).union(s.bbox()) for s in self.world.stickmen if id(s) in before]
        dirty += before.values()  # stickmen that left: erase where they were
        cb = self.couch.bbox()
        self.couch.update()
        if cb is not None:
            dirty.append(cb)
        mb = self.music.bbox()
        self.music.update()
        if mb is not None:
            dirty.append(mb)
        fb = self.fight.bbox()
        self.fight.update()
        fb2 = self.fight.bbox()
        dirty += [r for r in (fb, fb2) if r is not None]
        if self.music.error:
            self.director.note_event(tr(self.music.error))
            self.music.error = ""
        for s in gone:
            self._drop_bubble(s.key)
            if self.dragging is s:
                self.dragging = None
            self.director.note_event(tr("ev.left", name=s.name))
        if gone:
            self._update_present()
        self._update_bubbles()

        if self.debug and self.world.consume_support_changes():
            for o in self.overlays:
                o.update()
        else:
            for o in self.overlays:
                o.invalidate(dirty)
        if self.debug and self.hud_overlay is not None:
            self.hud_overlay.update(HUD_LOGICAL)

    # ---------------------------------------------------------------- mouse
    def _update_mouse(self) -> None:
        """The overlays are click-through except while the cursor is over a
        stickman (or one is being dragged): then the overlay under the cursor
        starts catching clicks so you can grab him."""
        cx, cy = win32.cursor_pos()
        if self.dragging is not None:
            if not win32.left_button_down():  # release happened outside our windows
                self.end_drag()
            else:
                self.dragging.drag_target.x, self.dragging.drag_target.y = cx, cy
        for s in self.world.stickmen:  # grab halo only for whoever the cursor is near
            b = s.bbox()
            s.show_halo = b.left - s.h * 0.5 <= cx <= b.right + s.h * 0.5 and b.top - s.h * 0.5 <= cy <= b.bottom + s.h * 0.5
        if self.settings_open():  # modal settings: the desktop stays click-through
            hot = False
        else:
            hot = self.dragging is not None or self.world.pick(cx, cy, slack=0.04) is not None
        for o in self.overlays:
            o.set_interactive(hot and (o.phys.contains(cx, cy) or self.dragging is not None))

    def begin_drag(self, overlay: OverlayWindow) -> None:
        cx, cy = win32.cursor_pos()
        s = self.world.pick(cx, cy, slack=0.06)
        if s is None:
            return
        s.grab(cx, cy)
        self.dragging = s
        overlay.setCursor(Qt.CursorShape.ClosedHandCursor)

    def end_drag(self) -> None:
        if self.dragging is None:
            return
        s = self.dragging
        s.release()
        self.dragging = None
        for o in self.overlays:
            if o.interactive:
                o.setCursor(Qt.CursorShape.OpenHandCursor)
        speed = s.vel.length() / s.h  # body-heights per second
        if self.roster.settings.react_to_throws and speed > 9:
            # comment once they've landed (or after a moment if still flying)
            QTimer.singleShot(900, lambda: self.director.react(s.key, tr("ev.threw", name=s.name)))

    def _on_widgets(self, found: dict) -> None:
        if not self.roster.settings.widgets:
            found = {}
        if self.scene.set_widgets(found):
            self.world.on_scene_refreshed()
            if self.debug:
                for o in self.overlays:
                    o.update()

    def _poll_scene(self) -> None:
        changed = self.scene.refresh()
        if changed and getattr(self.scene, 'extra', None):
            self.scene._rebuild()  # keep app-made surfaces (couch) after a window change
        self.couch.poll()
        # tell the widget scanner which windows to look into (topmost first)
        self.widget_scanner.set_targets(
            [(w.hwnd, (w.rect.left, w.rect.top, w.rect.right, w.rect.bottom), w.class_name) for w in self.scene.windows
             if any(m.work.intersects(w.rect) for m in self.scene.monitors)])
        self.world.on_scene_refreshed()
        if changed and self.debug:
            for o in self.overlays:
                o.update()
        self._ticks += 1
        if self._ticks % 40 == 0:  # other apps may claim topmost too; re-assert
            for o in self.overlays:
                win32.keep_topmost(o.hwnd)

    # ------------------------------------------------------------ characters
    def present_keys(self) -> list[str]:
        return [s.key for s in self.world.stickmen if not s.leaving]

    def summon(self, key: str) -> bool:
        """Bring a character onto the desktop. Returns True if newly spawned."""
        spec = self.roster.characters.get(key)
        if spec is None or self.world.get(key) is not None:
            return False
        scale = self.hud_overlay.scale if self.hud_overlay else 1.0
        s = self.world.spawn(key, spec.display_name, spec.color, config.STICKMAN_HEIGHT_DIP * scale * spec.size)
        self._apply_spec(s)
        self._entrance(s)
        log.info("Summoned %s", spec.display_name)
        self.director.note_event(tr("ev.arrived", name=spec.display_name))
        self._update_present()
        return True

    def _entrance(self, s) -> None:
        """Arrive in style: drop in from the top, walk / run in from a side, or jump up from below."""
        from .anim.mathutil import Vec
        from .anim.stickman import State
        mode = random.choices(("drop", "side", "bottom"), (0.35, 0.40, 0.25))[0]
        if mode == "drop":
            return
        sc = self.scene
        mon = next((m for m in sc.monitors if m.primary), sc.monitors[0] if sc.monitors else None)
        if mon is None:
            return
        wa = mon.work
        floor = sc.floor_under((wa.left + wa.right) / 2, wa.bottom - 2)
        if floor is None:
            return
        h = s.h
        if mode == "side":
            left = random.random() < 0.5
            x = max(floor.x0, wa.left) + 2 if left else min(floor.x1, wa.right) - 2
            s.pos = Vec(x, floor.y)
            s.vel = Vec()
            s.land(floor)
            s.land_impact, s.dust = 0.0, []
            s.facing = 1 if left else -1
            s.go_to(x + s.facing * random.uniform(2.5, 6.0) * h, max_seconds=6.0, run=random.random() < 0.5)
            if s.state != State.WALKING:
                s.set_state(State.WALKING)
        else:  # from below: hop up out of the taskbar
            x = random.uniform(max(floor.x0, wa.left) + 2 * h, min(floor.x1, wa.right) - 2 * h)
            s.pos = Vec(x, floor.y + 1.3 * h)
            s.start_fall(Vec(random.uniform(-1.0, 1.0) * h, -7.2 * h))
            s.jumping = True

    def quick_ask_at_cursor(self) -> None:
        """Right-click on a stickman: a small box for a message just to him."""
        cx, cy = win32.cursor_pos()
        s = self.world.pick(cx, cy, slack=0.06)
        if s is None:
            return
        spec = self.roster.characters.get(s.key)
        if spec is None:
            return
        s.face_toward(cx)
        s.talk(4.0)
        box = QuickAsk(s.key, spec.display_name, spec.color)
        box.sent.connect(lambda key, text: self.director.handle(text, None, to=[key]))
        box.open_at_cursor()
        self._quick_box = box  # keep a reference while it's open

    def _maybe_toast(self, e) -> None:
        """While the chat is closed, lines from them (and task results) pop up in a corner."""
        st = self.roster.settings
        if not st.notify or self.hidden or self.chat.isVisible():
            return
        if e.kind == "char":
            spec = self.roster.characters.get(e.speaker)
            name, color = (spec.display_name, spec.color) if spec else (e.speaker, "#888888")
        elif e.kind == "event" and e.text[:1] in ("✅", "❌", "⚠", "🏆", "⏰"):
            name, color = "StickAI", "#8d929c"
        else:
            return
        self.toasts.corner = st.notify_corner
        self.toasts.show(name, color, e.text, sound=st.notify_sound)

    def _update_present(self) -> None:
        self.chat.set_present([(s.name, s.color) for s in self.world.stickmen if not s.leaving])

    # ---- banter
    def _schedule_banter(self) -> None:
        lo, hi = self.roster.settings.banter_every
        self.banter_timer.start(int(random.uniform(lo, hi) * 1000))

    def _banter(self) -> None:
        if self.roster.settings.banter and not self.paused and not self.hidden and len(self.present_keys()) >= 2:
            if not self.director.banter():
                # busy (or you were just chatting): try again soon rather than
                # waiting a whole interval
                self.banter_timer.start(int(random.uniform(25, 45) * 1000))
                return
        self._schedule_banter()

    def _apply_spec(self, s) -> None:
        spec = self.roster.characters.get(s.key)
        if spec is None:
            return
        s.name, s.color = spec.display_name, spec.color
        s.walk_speed, s.climb_chance, s.restlessness = spec.walk_speed, spec.climb_chance, spec.restlessness
        s.energy = spec.energy
        scale = self.hud_overlay.scale if self.hud_overlay else 1.0
        s.set_height(config.STICKMAN_HEIGHT_DIP * scale * spec.size)  # grows/shrinks live
        if s.key in self.bubbles:
            self.bubbles[s.key].set_color(spec.color)

    def dismiss(self, key: str) -> None:
        """Send a character home (they leap off the top of the screen)."""
        s = self.world.get(key)
        if s is not None:
            if self.dragging is s:
                self.dragging = None
            s.leave()

    def describe(self, key: str) -> str:
        """Where a character is, in words, for the AI prompt."""
        s = self.world.get(key)
        if s is None:
            return "not here"
        st = s.state.name
        if st == "DRAGGED":
            return "being held up in the air by the user's mouse cursor"
        if time.monotonic() - s.born < 3.0:
            return "just arrived, dropping in from the top of the screen"
        if st == "FALLING":
            return "falling through the air"
        if s.wall is not None:
            return "climbing up the side of a window"
        thing = {"heading": "a heading of a web page", "image": "a picture on a web page",
                 "button": "a button"}.get(getattr(s.support, "kind", ""), "a search bar / text field")
        if s.support is not None and s.support.hwnd == COUCH:
            return "sitting on the couch, watching a video together with the user"
        if st == "HOP":
            return "jumping onto something inside a window"
        if st == "SITTING" and s.support is not None and s.support.widget:
            return f"sitting on {thing} inside a window, legs dangling"
        if s.support is not None and s.support.widget:
            return f"standing on {thing} inside a window"
        if st == "SITTING":
            return "sitting on top of a window, legs dangling" if s.sit_edge else "sitting down, resting"
        if s.running:
            return "running around"
        if s.support is not None:
            if s.support.is_floor:
                return "walking on the taskbar at the bottom of the screen"
            w = self.scene.window(s.support.hwnd)
            if w is not None and self.roster.settings.share_window_titles:
                return f'standing on top of the window "{w.title[:60]}"'
            return "standing on top of an open window"
        return "on the desktop"

    def say(self, key: str, text: str, action: str = "none", to: str = "user", force_bubble: bool = False) -> None:
        """Show a line above a character. force_bubble: a full bubble whatever the bubble
        setting (reminders and tool results you need to read right there)."""
        s = self.world.get(key)
        if s is None:
            return
        mode = "off" if self.hidden else ("full" if force_bubble else self.roster.settings.bubbles)
        if mode != "off":
            b = self.bubbles.get(key)
            if b is None:
                b = self.bubbles[key] = SpeechBubble(s.color)
            if mode == "full":
                b.say(text)
            else:  # just a sign that they said something: ? for questions, ! otherwise
                b.indicate("?" if text.rstrip().endswith("?") else "!")
        s.talk(reading_time(text) * 0.8)
        other = self.world.get(to)
        if other is not None and other is not s:
            s.face_toward(other.pos.x)   # look at who you're talking to...
            other.face_toward(s.pos.x)   # ...and they look back
        self.act(key, action, to)
        self._update_bubbles()

    def act(self, key: str, action: str, to: str = "") -> None:
        s = self.world.get(key)
        if s is None:
            return
        if action == "approach":
            other = self.world.get(to)
            if other is not None and other is not s and other.grounded and s.grounded \
                    and abs(other.pos.y - s.pos.y) < 4:  # same surface: walk over
                s.go_to(other.pos.x - (0.7 * s.h if other.pos.x > s.pos.x else -0.7 * s.h))
            elif other is not None:
                s.face_toward(other.pos.x)
        elif action == "wave":
            s.wave()
        elif action == "sit":
            s.sit(random.uniform(8.0, 16.0))
        elif action == "remove_couch":
            self.couch.remove()
        elif action == "watch":
            self.couch.start(manual=True)
        elif action == "fight":
            pair = [key, to] if to in self.roster.characters and to != key else None
            if pair:
                self.summon(to)
            self.fight.start(pair)
        elif action == "music":
            st = self.roster.settings
            err = self.music.start(s, st.music_file, st.music_bpm)
            self.director.note_event(tr(err, name=s.name) if err else tr("music.started", name=s.name))
        elif action == "run":
            s.run(random.uniform(2.5, 5.0))
        elif action == "jump":
            s.jump()
        elif action == "leave":
            # let the goodbye bubble be read first
            QTimer.singleShot(1800, lambda: s.leave() if s in self.world.stickmen else None)

    def show_thinking(self, key: str, on: bool) -> None:
        s = self.world.get(key)
        b = self.bubbles.get(key)
        if on and s is not None and self.roster.settings.bubbles != "off" and not self.hidden:
            if b is None:
                b = self.bubbles[key] = SpeechBubble(s.color)
            b.think()
            self._update_bubbles()
        elif not on and b is not None and b.thinking:
            self._drop_bubble(key)

    def _drop_bubble(self, key: str) -> None:
        b = self.bubbles.pop(key, None)
        if b is not None:
            b.close()
            b.deleteLater()

    def _update_bubbles(self) -> None:
        for key, b in list(self.bubbles.items()):
            s = next((x for x in self.world.stickmen if x.key == key), None)
            if s is None or b.expired:
                self._drop_bubble(key)
                continue
            head = s.joints["head"]
            mon = next((m for m in self.scene.monitors if m.bounds.contains(head.x, head.y)), None) \
                or next((m for m in self.scene.monitors if m.primary), None)
            if mon is not None:
                b.follow(head.x, head.y - s.head_r, mon.work)

    def _reload_roster(self) -> None:
        # Editors often save by replacing the file, which drops the watch.
        if str(self.characters_path) not in self._watcher.files():
            self._watcher.addPath(str(self.characters_path))
        try:
            roster = load_roster(self.characters_path)
        except ValueError as e:
            log.warning("characters.toml not reloaded: %s", e)
            self.chat.set_status(f"characters.toml has an error (kept the old one): {e}")
            self.tray.showMessage("StickAI", f"characters.toml has an error:\n{e}", QSystemTrayIcon.MessageIcon.Warning)
            return
        if roster != self.roster:
            self.apply_roster(roster)
            log.info("characters.toml reloaded")
            self.chat.set_status(tr("st.reloaded"))

    def _apply_spellcheck(self) -> None:
        names = [n for c in self.roster.characters.values() for n in (c.key, c.display_name, *c.aliases)]
        names += [w for phrase in names + list(self.roster.settings.group_words) for w in phrase.split()]
        self.chat.input.set_spellcheck(self.roster.settings.spellcheck, names)

    def apply_roster(self, roster: Roster) -> None:
        """Make the running app match a (new) roster: language, looks, timers..."""
        old = self.roster.settings
        old_chars, new_chars = self.roster.characters, roster.characters
        self.roster = roster
        new = roster.settings
        for s in self.world.stickmen:
            self._apply_spec(s)
        if new.language != old.language:
            set_language(new.language)
            self.chat.retranslate()
            self._build_tray_menu()
        self._update_present()
        self._apply_spellcheck()
        if (new.vscode, new.coder, new.language) != (old.vscode, old.coder, old.language):
            self.code.apply_settings()
        if [(k, c.display_name, c.color) for k, c in new_chars.items()] != \
                [(k, c.display_name, c.color) for k, c in old_chars.items()] or new.language != old.language:
            self.chat.build_chips()
        if new.chat_on_top != old.chat_on_top:
            self.chat.apply_on_top(new.chat_on_top)
        if new.banter_every != old.banter_every:
            self._schedule_banter()
        if new.hotkey != old.hotkey or new.hide_hotkey != old.hide_hotkey:
            self._register_hotkey()
        if new.widgets != old.widgets or new.web_surfaces != old.web_surfaces:
            self.widget_scanner.set_enabled(new.widgets, new.web_surfaces)
        if new.bubbles != old.bubbles:
            for key in list(self.bubbles):
                self._drop_bubble(key)

    # ---- seeing the screen
    def grab_screen(self):
        """QImage of the screen the cursor is on (stickmen and all)."""
        cx, cy = win32.cursor_pos()
        scr = next((q for q in QGuiApplication.screens() if o_contains(self, q, cx, cy)), None) \
            or QGuiApplication.primaryScreen()
        # remember which monitor (physical px) the picture shows: "click at 40%, 60% of it" maps back here
        o = next((o for o in self.overlays if o.qscreen is scr and o.monitor is not None), None)
        self.last_capture_bounds = o.monitor.bounds if o is not None else None
        return scr.grabWindow(0).toImage()

    def grab_rect(self, rect):
        """Pixels of a physical-pixel rectangle -> (QImage, image px per physical px) or None."""
        cx, cy = (rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2
        o = next((o for o in self.overlays if o.monitor is not None and o.monitor.bounds.contains(cx, cy)), None)
        if o is None:
            return None
        mon, scr = o.monitor.bounds, o.qscreen
        dpr = scr.devicePixelRatio() or 1.0
        left, top = max(rect.left, mon.left), max(rect.top, mon.top)
        right, bottom = min(rect.right, mon.right), min(rect.bottom, mon.bottom)
        if right - left < 8 or bottom - top < 8:
            return None
        img = scr.grabWindow(0, int((left - mon.left) / dpr), int((top - mon.top) / dpr),
                             int((right - left) / dpr), int((bottom - top) / dpr)).toImage()
        if img.isNull() or img.width() == 0:
            return None
        # the grab starts at (left, top); callers measure from rect's corner
        k = img.width() / (right - left)
        if left != rect.left or top != rect.top:
            from PySide6.QtGui import QPainter as _P
            full = img.__class__(int(rect.width * k), int(rect.height * k), img.format())
            full.fill(0)
            p = _P(full)
            p.drawImage(int((left - rect.left) * k), int((top - rect.top) * k), img)
            p.end()
            img = full
        return img, k

    def own_rects(self) -> list:
        """Screen areas covered by our own stickmen and bubbles (not game targets!)."""
        rects = [s.bbox() for s in self.world.stickmen]
        overlay_ids = {o.hwnd for o in self.overlays}
        for w in QApplication.topLevelWidgets():  # chat, settings, dialogs, speech bubbles
            if w.isVisible() and int(w.winId()) not in overlay_ids:
                r = win32.window_rect(int(w.winId()))
                if r is not None:
                    rects.append(r)
        return rects

    def capture_screen(self, max_side: int = 1600):
        """Screenshot as (jpeg bytes, mime) for Gemini, or None."""
        img = self.grab_screen()
        if img.isNull():
            return None
        if max(img.width(), img.height()) > max_side:
            img = img.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "JPEG", 80)
        return bytes(ba.data()), "image/jpeg"

    def attach_screenshot(self) -> None:
        """👁 in the chat: step aside, snap the screen, attach it to the message."""
        self.chat.hide()
        def snap():
            img = self.grab_screen()
            self.chat.show()
            self.chat.raise_()
            self.chat.activateWindow()
            self.chat.set_attachment(img)
        QTimer.singleShot(300, snap)

    def confirm_quit(self) -> None:
        if confirm(self.chat, tr("quit.title"), tr("quit.question"), tr("quit.yes"), tr("dlg.cancel"),
                   kind="warning", accept_default=False):
            self.qapp.quit()

    def settings_open(self) -> bool:
        return self.settings_dialog is not None and self.settings_dialog.isVisible()

    def _tray_talk(self) -> None:
        if self.hidden:
            self.toggle_hidden()
            return
        if self.settings_open():
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
        else:
            self.chat.summon()

    def toggle_chat(self) -> None:
        """Hotkey: show/hide the chat, but never hide it from under open settings."""
        if self.hidden:  # the chat hotkey also brings everybody back
            self.toggle_hidden()
            if not self.chat.isVisible():
                self.chat.summon()
            return
        if self.settings_open():
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
        else:
            self.chat.toggle()

    def toggle_hidden(self) -> None:
        """Hide hotkey: make the stickmen, their bubbles, the chat and the
        settings window vanish (and freeze), or bring it all back as it was."""
        if not self.hidden:
            self._restore_settings = self.settings_open()
            self._restore_chat = self.chat.isVisible()
            if self.dragging is not None:
                self.end_drag()
            if self._restore_settings:
                self.settings_dialog.hide()
            self.chat.hide()
            for key in list(self.bubbles):
                self._drop_bubble(key)
            for o in self.overlays:
                o.set_interactive(False)
                o.hide()
            self.hidden = True
            log.info("Hidden")
        else:
            self.hidden = False
            self._last = time.perf_counter()  # no time jump after waking up
            for o in self.overlays:
                o.show()
                win32.keep_topmost(o.hwnd)
                o.update()
            if self._restore_chat or self._restore_settings:
                self.chat.show()
                self.chat.raise_()
            if self._restore_settings and self.settings_dialog is not None:
                self.settings_dialog.show()
                self.settings_dialog.raise_()
                self.settings_dialog.activateWindow()
            log.info("Shown")
        self._build_tray_menu()

    def open_settings(self, tab: int = 0) -> None:
        if self.hidden:
            self.toggle_hidden()
        if self.settings_open():
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
            return
        if self.dragging is not None:
            self.end_drag()
        self.chat.summon()  # settings sit on top of the chat window, which owns them
        if self.settings_dialog is None:
            self.settings_dialog = SettingsDialog(self)
            self.settings_dialog.tabs.setCurrentIndex(tab)
        else:
            self.settings_dialog.load_from_app()
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def reopen_settings(self, tab: int = 0) -> None:
        """Rebuild the settings window (after a language change)."""
        if self.settings_dialog is not None:
            pos = self.settings_dialog.pos()
            self.settings_dialog.hide()
            self.settings_dialog.deleteLater()
            self.settings_dialog = None
            self.open_settings(tab)
            self.settings_dialog.move(pos)
            self.settings_dialog._set_status(tr("st.saved"))

    def forget_everything(self) -> None:
        self.memory.forget()
        self.director.events.clear()
        self.chat.clear_messages()

    def _register_hotkey(self) -> None:
        err = self.hotkey.register(self.roster.settings.hotkey)
        hide_err = self.hide_hotkey.register(self.roster.settings.hide_hotkey)
        if hide_err:
            log.warning(hide_err)
            self.tray.showMessage("StickAI", hide_err, QSystemTrayIcon.MessageIcon.Warning)
        if err:
            log.warning(err)
            self.tray.showMessage("StickAI", f"{err}\nYou can still click the tray icon to talk.",
                                  QSystemTrayIcon.MessageIcon.Warning)

    def _shutdown(self) -> None:
        self.widget_scanner.stop()
        self.hotkey.unregister()
        self.hide_hotkey.unregister()
        self.director.shutdown()
        self.code.shutdown()

    # -------------------------------------------------------------- tray/UI
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(_tray_icon(config.COLORS["yellow"]))
        self.tray.setToolTip("StickAI  (click to talk)")
        self.tray.activated.connect(
            lambda reason: self._tray_talk() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self._build_tray_menu()
        self.tray.show()

    def _build_tray_menu(self) -> None:
        menu = QMenu()
        menu.setStyleSheet(theme.MENU_STYLE)
        act_talk = QAction(tr("tray.talk"), menu)
        act_talk.triggered.connect(self._tray_talk)
        act_settings = QAction(tr("tray.settings"), menu)
        act_settings.triggered.connect(lambda: self.open_settings())
        act_hide = QAction(tr("tray.show") if self.hidden else tr("tray.hide"), menu)
        act_hide.triggered.connect(self.toggle_hidden)
        menu.addAction(act_talk)
        menu.addAction(act_settings)
        menu.addAction(act_hide)
        menu.addSeparator()
        act_debug = QAction(tr("tray.debug"), menu, checkable=True, checked=self.debug)
        act_debug.toggled.connect(self._set_debug)
        act_pause = QAction(tr("tray.pause"), menu, checkable=True, checked=self.paused)
        act_pause.toggled.connect(lambda v: setattr(self, "paused", v))
        act_respawn = QAction(tr("tray.respawn"), menu)
        act_respawn.triggered.connect(self._respawn_all)
        act_quit = QAction(tr("tray.quit"), menu)
        act_quit.triggered.connect(self.qapp.quit)
        for a in (act_debug, act_pause, act_respawn):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self._tray_menu = menu

    def _set_debug(self, value: bool) -> None:
        self.debug = value
        for o in self.overlays:
            o.update()

    def _respawn_all(self) -> None:
        for s in self.world.stickmen:
            s.respawn(*self.world.spawn_point(s.h))

    def hud_lines(self) -> list[str]:
        sc = self.scene
        lines = [f"StickAI v0.2   {self._fps:5.1f} fps   windows {len(sc.windows)}   "
                 f"platforms {len(sc.platforms)}   walls {sum(w.climbable for w in sc.walls)}",
                 "drag a stickman with the mouse; throw him by letting go mid-swing"]
        fg = sc.foreground
        if fg is not None:
            r = fg.rect
            lines.append(f"active: {fg.title[:40]}  ({r.left},{r.top})-({r.right},{r.bottom})")
        else:
            lines.append("active: (desktop / taskbar)")
        for s in self.world.stickmen:
            on = ("wall" if s.wall is not None else "air") if s.support is None else ("floor" if s.support.is_floor else
                                                  (sc.window(s.support.hwnd).title[:24] if sc.window(s.support.hwnd) else "window"))
            lines.append(f"{s.name:<6} {s.state.name:<8} x={s.pos.x:7.1f} y={s.pos.y:7.1f}  on {on}")
        return lines

    # ------------------------------------------------------------ dev aids
    def _trace(self) -> None:
        for s in self.world.stickmen:
            log.info("trace %s %s pos=(%.1f, %.1f) facing=%d speed=%.1f support=%s",
                     s.name, s.state.name, s.pos.x, s.pos.y, s.facing, s.speed,
                     None if s.support is None else (s.support.hwnd, s.support.y, s.support.x0, s.support.x1))

    def _exit_with_report(self) -> None:
        log.info("fps=%.1f windows=%d platforms=%d", self._fps, len(self.scene.windows), len(self.scene.platforms))
        if self.args.snapshot and self.hud_overlay is not None:
            self._save_snapshot(self.args.snapshot)
        self.qapp.quit()

    def _save_snapshot(self, path: str) -> None:
        """Render the primary overlay onto a checkerboard and save it."""
        o = self.hud_overlay
        img = o.grab()
        bg = QPixmap(img.size())
        bg.setDevicePixelRatio(img.devicePixelRatio())
        bg.fill(QColor(40, 44, 52))
        p = QPainter(bg)
        p.drawPixmap(0, 0, img)
        p.end()
        bg.save(path)
        log.info("snapshot -> %s", path)

    def run(self) -> int:
        return self.qapp.exec()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="StickAI desktop stickmen")
    ap.add_argument("--debug", action="store_true", help="show window outlines / HUD at start (tray menu toggles it)")
    ap.add_argument("--no-debug", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--spawn", nargs="*", default=None,
                    help="stickmen to spawn at start (default: start_with in characters.toml)")
    ap.add_argument("--characters", default=str(PROJECT_DIR / "characters.toml"),
                    help="path to the character sheet")
    ap.add_argument("--exit-after", type=float, default=0, help="quit after N seconds (testing)")
    ap.add_argument("--snapshot", help="with --exit-after: save the primary overlay to this PNG")
    ap.add_argument("--trace", action="store_true", help="log stickman state twice a second")
    return ap.parse_args(argv)


def _message_box(text: str) -> None:
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, text, "StickAI", 0x40)  # MB_ICONINFORMATION


def _setup_logging() -> None:
    """Log to the console; without one (StickAI.exe / pythonw) to %LOCALAPPDATA%/StickAI/stickai.log."""
    fmt = "%(asctime)s %(levelname)s %(message)s"
    if sys.stderr is not None:
        logging.basicConfig(level=logging.INFO, format=fmt, datefmt="%H:%M:%S")
        return
    from logging.handlers import RotatingFileHandler
    folder = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "StickAI"
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / "stickai.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt="%Y-%m-%d %H:%M:%S", handlers=[handler])

    def hook(kind, value, tb):  # crashes go to the log instead of a console nobody sees
        logging.getLogger("stickai").critical("Unhandled error", exc_info=(kind, value, tb))
    sys.excepthook = hook


def _already_running() -> bool:
    """A second StickAI would fight the first one over hotkeys and the VS Code link."""
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    global _MUTEX
    _MUTEX = k32.CreateMutexW(None, False, r"Local\StickAI.single-instance")
    return k32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = parse_args(argv)
    if args.exit_after is None and _already_running():
        try:
            set_language(load_roster(Path(args.characters)).settings.language)
        except Exception:  # noqa: BLE001 - English is fine for this one message
            pass
        _message_box(tr("app.already_running"))
        return 0
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C in the console quits
    try:
        app = StickApp(args)
    except SystemExit as e:
        if sys.stderr is None and e.code not in (0, None):
            _message_box(str(e.code))
        raise
    return app.run()
