"""Conversation director.

Every Gemini request voices *everyone present* at once and returns JSON:

    {"replies": [{"speaker": "red", "to": "blue", "text": "...", "action": "approach"}],
     "image_note": "a photo of a cat on a keyboard"}

Request kinds:
  chat     the user said something (optionally with a picture)
  banter   nobody spoke for a while: the characters chat among themselves
  react    something happened (e.g. the user threw someone across the screen)
  summary  fold older chat into long-term memory (memory/memory.json)

Requests run one at a time; user messages always win over banter.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage

from ..i18n import tr
from .. import skills
from .characters import LANGUAGES
from ..tools import control
from ..tools.files import Workspace
from .gemini import GeminiClient, GeminiResult
from .memory import Entry, MemoryStore

if TYPE_CHECKING:
    from ..app import StickApp

log = logging.getLogger("stickai.director")

STOP_WORDS = ("stop", "enough", "quit", "стоп", "хватит", "остановись", "dosť", "prestaň", "stoj")
ACTIONS = ("none", "wave", "jump", "run", "sit", "approach", "leave", "music", "watch", "remove_couch", "fight", "cards")
MAX_IMAGE_SIDE = 1024
# A message that mentions the clipboard (only then is it read, and only with the setting on).
CLIPBOARD_WORDS = re.compile(
    r"clipboard|copied|i copy|what i cop|буфер|скопировал|скопировала|скопированн|копирнул|schránk|skopíroval|skopírovan",
    re.IGNORECASE)
MAX_CLIPBOARD_SENT = 4000
MAX_ROUNDS = 4  # follow-up rounds per message: act, see the result, act again...
# a message that asks them to DO something: planned with the smarter model at a low temperature
ACTION_WORDS = re.compile(
    r"(?<!\w)(?:открой|откр|запусти|включи|выключи|найди|поищи|загугли|закрой|сверни|скрой|покажи|разверни|перейди|"
    r"напиши|отправь|поставь|прокрути|нажми|сделай|создай|переключи|зайди|верни|убей|заверши|"
    r"open|launch|start|play|find|search|google|close|hide|show|go to|type|write|send|click|press|scroll|run|"
    r"turn|switch|commit|push|pull|git|otvor|spusti|nájdi|zatvor|pusti|napíš|klikni|prepni)", re.IGNORECASE)
# The music show (boxes + the user's song) only when the message itself asks a character to play
# music. Models love to add it to anything ("open a YouTube video" -> music), so this is checked in code.
MUSIC_ASK = re.compile(r"музык|сыграй|сыгра[ей]|поиграй|спой|песн|мелоди|music|play (?:us |me )?(?:a |some )?(?:song|tune)|"
                       r"sing|song|hudb|zahraj|zaspievaj|pesni", re.IGNORECASE)
# ...but "put on music on YouTube / Spotify" is the user's own player, not the show
OTHER_PLAYER = re.compile(r"ютуб|youtube|you tube|spotify|спотиф|видео|video|плейлист|playlist|трек|track|radio|радио",
                          re.IGNORECASE)
# Fighting starts only when the message asks for it (checked in code, like the music show).
FIGHT_ASK = re.compile(r"(?<!\w)(?:дерит|подерит|подерис|дерис|драк[аиу]|дратьс|подрат|в бой|бой\b|бейт|побей|бей(?!\w)|ударь|врежь|атакуй|attack|punch him|kick him|отпизд|"
                       r"отлупи|вмаж|вломи|сразит|сражайт|fight|brawl|battle|beat (?:him|her|them|each other|up)|"
                       r"bitk|pobi|bijte)", re.IGNORECASE)
FIGHT_STOP = re.compile(r"(?:не|хватит|прекратите|прекрати|перестаньте|stop|no more|quit|prestaňte|dosť)\s+"
                        r"(?:\w+\s+)?(?:дерит|драть|драк|бить|fight|bit)", re.IGNORECASE)
# Scrolling direction said in words ("кручу вверх"): small models often leave the field out (-> down)
SCROLL_UP = re.compile(r"вверх|наверх|назад к|к началу|в начало|выше|(?<![a-z])up(?![a-z])|to the top|back to|"
                       r"hore|nahor|vyššie", re.IGNORECASE)
SCROLL_DOWN = re.compile(r"вниз|ниже|дальше|к концу|(?<![a-z])down(?![a-z])|further|to the bottom|dole|nižšie",
                         re.IGNORECASE)
NEW_WINDOW_WORDS = re.compile(r"new (browser |chrome |edge |firefox )?window|нов\w* окн|nov\w* okn", re.IGNORECASE)
# Words that only call someone over. A message made of names + these is "just a summon"
# (with greet_on_arrival off, nobody answers it).
SUMMON_FILLER = {
    "hi", "hey", "hello", "hiya", "yo", "sup", "howdy", "oi", "come", "here", "over", "out", "on", "in", "get",
    "please", "pls", "plz", "and", "&", "you", "now", "back",
    "привет", "приветик", "здравствуй", "здравствуйте", "хай", "эй", "иди", "идите", "сюда", "приходи", "приходите",
    "давай", "давайте", "ко", "мне", "пожалуйста", "и", "ну",
    "ahoj", "čau", "cau", "zdravím", "zdravim", "hej", "poď", "pod", "poďte", "podte", "sem", "prosím", "prosim", "a",
}


def _encode(img: QImage, fmt: str, quality: int = -1) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, fmt, quality)
    buf.close()
    return bytes(ba.data())


def _ago(seconds: float) -> str:
    m = seconds / 60
    if m < 90:
        return f"{int(m)} minutes"
    h = m / 60
    if h < 36:
        return f"{int(h)} hours"
    return f"{int(h / 24)} days"


class Director(QObject):
    status = Signal(str)        # one-line status for the chat window
    message = Signal(object)    # memory.Entry to show in the chat window
    thinking = Signal(bool)

    def __init__(self, app: "StickApp", secrets_path: Path, memory: MemoryStore):
        super().__init__()
        self.app = app
        self.memory = memory
        self.client = GeminiClient(secrets_path)
        self.client.finished.connect(self._on_result)
        self.events: deque[tuple[float, str]] = deque(maxlen=10)  # recent happenings
        self._pending: dict | None = None
        self._queued: tuple[str, QImage | None] | None = None
        self._last_user = 0.0
        self._last_reaction = 0.0

    @property
    def roster(self):
        return self.app.roster

    @property
    def settings(self):
        return self.app.roster.settings

    def name(self, key: str) -> str:
        c = self.roster.characters.get(key)
        return c.display_name if c else key

    # ------------------------------------------------------------ parsing
    def mentioned(self, text: str) -> tuple[list[str], bool]:
        low = text.lower()

        def has(word: str) -> bool:
            return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", low) is not None

        keys = [k for k, c in self.roster.characters.items() if any(has(n) for n in c.names)]
        return keys, any(has(w) for w in self.settings.group_words)

    # ------------------------------------------------------------- input
    def cancel_thinking(self) -> bool:
        """The stop button: drop the answer being thought up (and any follow-up rounds)."""
        if self._pending is None or self._pending.get("tag") != "chat":
            return False
        self._finish_pending()
        self._queued = None
        self._last_user = time.monotonic()  # like a new message: follow-up rounds of a plan stop too
        self.status.emit(tr("st.cancelled"))
        return True

    def handle(self, text: str, image: QImage | None = None, to: list[str] | None = None) -> None:
        """A message from the user (the chat window). `to`: characters it's for (chips, right-click)."""
        text = text.strip()
        if not text and image is None:
            return
        low = text.lower()
        self._fight_stopped = False
        if any(re.search(rf"(?<!\w){w}(?!\w)", low) for w in STOP_WORDS):
            self.app.tasks.cancel()  # games and plans stop at once; they still get to answer below
            self.app.music.stop()
            self.app.couch.end()
            self.app.cards.end()
            self._fight_stopped = self.app.fight.stop()
        keys, group = self.mentioned(text)
        targets = list(self.roster.characters) if group and not keys else keys
        if to:  # picked in the chat / right-clicked: those, plus anyone named in the text
            targets = list(dict.fromkeys([k for k in to if k in self.roster.characters] + targets))

        img_rel, payload = "", None
        if image is not None and not image.isNull():
            if max(image.width(), image.height()) > MAX_IMAGE_SIDE:
                image = image.scaled(MAX_IMAGE_SIDE, MAX_IMAGE_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            img_rel = self.memory.save_image(_encode(image, "PNG"))
            payload = (_encode(image.convertToFormat(QImage.Format.Format_RGB888), "JPEG", 88), "image/jpeg")
        entry = self.memory.add(Entry("user", text, to=",".join(targets), image=img_rel))
        self.message.emit(entry)
        self._last_user = time.monotonic()
        arrived = [k for k in targets if self.app.summon(k)]
        job = self.app.code.wants(text) if image is None else None
        if job:  # "explain this code" / "comment it" / "what's this error" while VS Code is in use
            key = next(iter(targets), self.app.code.coder())  # whoever you named, else the coder
            self.app.summon(key)
            self.app.code.run_from_chat(job, key)
            return

        present = self.app.present_keys()
        if not present:
            names = ", ".join(c.display_name for c in self.roster.characters.values())
            self.status.emit(tr("st.nobody", names=names))
            return
        if image is None and self._quick_fight(text, [k for k in targets if k in present] or present):
            return  # "fight!" / "хватит драться": done locally, no AI request
        if self._pending is not None:
            if self._pending["tag"] == "chat":
                self._queued = (entry, payload)
                self.status.emit(tr("st.busy"))
                return
            self._pending["stale"] = True  # banter/summary in flight: ignore its result
            self._finish_pending()
        if arrived and not self.settings.greet_on_arrival and image is None and self._only_summon(text):
            return  # "silent arrival": they just drop in, no request and no hello
        if image is None and self._quick(entry, [k for k in targets if k in present] or present):
            return  # a common command: done locally, no AI request
        self._send_chat(entry, payload, [k for k in targets if k in present])

    def _command_text(self, text: str) -> str:
        """The message without character names / group words: "Blue, пауза!" -> "пауза"."""
        low = text
        phrases = list(self.settings.group_words)
        for c in self.roster.characters.values():
            phrases += list(c.names)
        for ph in sorted(phrases, key=len, reverse=True):
            low = re.sub(rf"(?<!\w){re.escape(ph)}(?!\w)", " ", low, flags=re.IGNORECASE)
        return " ".join(low.split()).strip(" ,;:-–—")

    def _quick(self, entry: Entry, who: list[str]) -> bool:
        """Common commands ("пауза", "открой загрузки", "закрой телеграм"...) run at once, with no
        AI request; the character just says a word. True if it was one."""
        if not entry.text or not who or not skills.enabled(self.settings):
            return False
        steps = skills.quick(self._command_text(entry.text), self.app)
        if not steps:
            return False
        key = who[0]
        self._last_chat, self._plan_user_t = (entry, [key]), self._last_user
        targets = self._control_lines()[1] if self.settings.control else {}
        line = Entry("char", random.choice(tr("quick.ok").split("|")), speaker=key, to="user")
        self.memory.add(line)
        self.message.emit(line)
        self.app.say(key, line.text, "none")
        log.info("quick command %r -> %s", entry.text, steps)
        # round 1: no automatic "look" request afterwards; a failure still gets one AI round to fix it
        self.app.tasks.propose_plan(key, steps, targets, 1, False)
        return True

    def _only_summon(self, text: str) -> bool:
        """Is the message nothing but names / group words / "hi, come here"?"""
        low = text.lower()
        phrases = list(self.settings.group_words)
        for c in self.roster.characters.values():
            phrases += [n.lower() for n in c.names]
        for ph in sorted(phrases, key=len, reverse=True):  # "yellow guy" before "yellow"
            low = re.sub(rf"(?<!\w){re.escape(ph)}(?!\w)", " ", low)
        return all(w in SUMMON_FILLER for w in re.findall(r"\w+", low))

    def note_event(self, text: str, show: bool = True) -> None:
        """Something happened on the desktop that they may talk about."""
        self.events.append((time.time(), text))
        if show:
            self.message.emit(self.memory.add(Entry("event", text)))

    # ----------------------------------------------------------- requests
    def _send(self, tag: str, prompt: str, thinking: list[str], schema: dict | None,
              system: str | None = None, image=None, owner: str | None = None, action: bool = False,
              **extra) -> bool:
        if not self.client.has_key(owner):
            self.status.emit(tr("err.no_key"))
            return False
        # Pro (if chosen) is spent only on replies to you; background chatter, reactions and
        # memory summaries use the cheaper Flash models.
        models = self.settings.models
        if tag != "chat":
            models = tuple(m for m in models if "pro" not in m) or ("gemini-3.5-flash-lite", "gemini-3.8-flash")
        temperature = self.settings.temperature
        if action:  # doing things: the smarter model first, and calm, exact (personality stays in the prompt)
            models = tuple(sorted(models, key=lambda m: "lite" in m))
            temperature = min(temperature, 0.35)
        rid = self.client.generate(models, system or self._system(), prompt, schema,
                                   temperature, image=image, tag=tag, owner=owner)
        self._pending = {"rid": rid, "tag": tag, "thinking": thinking, **extra}
        for k in thinking:
            self.app.show_thinking(k, True)
        self.thinking.emit(True)
        return True

    def _next_owner(self, present: list[str]) -> str | None:
        """Take turns: a request nobody in particular owns uses the next present character's key."""
        if not present:
            return None
        self._owner_turn = (getattr(self, "_owner_turn", -1) + 1) % len(present)
        return present[self._owner_turn]

    def _send_chat(self, entry: Entry, image, addressed: list[str], screenshot=None, scrolled: bool = False,
                   progress: list[str] | None = None, round_: int = 0) -> None:
        present = self.app.present_keys()
        self._last_chat = (entry, addressed)
        lines = self._situation(present)
        targets: dict = {}
        if self.settings.control:
            ctl, targets = self._control_lines()
            lines += [""] + ctl
            if progress and getattr(self, "_observed_hwnd", None):
                targets["observed"] = self._observed_hwnd  # click_button without a usable window id
                targets["buttons"] = getattr(self, "_observed_buttons", [])
                targets["button_rects"] = getattr(self, "_observed_rects", (None, []))
        lines += self._tool_lines(entry, addressed or present)
        if self.settings.files:
            ws = Workspace(self.settings.files_folder)
            lines += ["", f"Your folder ({ws.root.name}) contains:"] + [f"- {x}" for x in ws.listing()] \
                if ws.listing() else ["", "Your folder is empty so far."]
            for path, text in ws.recent_texts():
                lines += [f"--- {path} ---", text, "--- end ---"]
        lines += ["", f"User's new message: {entry.text or '(no text)'}"]
        if progress:
            lines += ["", f"YOU ALREADY ACTED on this message (round {round_} of {MAX_ROUNDS}). Results, in order:"]
            lines += progress
            lines.append(
                "Now: if the user's request is fully done, confirm it in ONE short line (or send no replies if you "
                "already said everything). If a step failed or something is still missing, say so honestly and add "
                "the next steps to fix it or finish (different approach if the same one failed). Don't repeat steps "
                "that worked. Don't greet again. Check the button list" + (" and screenshot" if screenshot is not None
                                                                          else "") +
                ": is what the user wanted really there / happening?" + (" The attached screenshot shows the screen "
                                                                          "now: check it." if screenshot is not None
                                                                          else ""))
        elif screenshot is not None and scrolled:
            image = screenshot
            lines.append("You just scrolled the page for the user; the attached image is the screen now. Briefly "
                         "tell the user what's visible (the useful part, not every detail). Don't scroll again "
                         "unless the user's message asked for more than one screen.")
        elif screenshot is not None:
            image = screenshot
            lines.append("You asked to look, so the attached image is a screenshot of the user's screen right "
                         "now. React to what you actually see, be specific. Don't ask to look again.")
        elif image is not None:
            lines.append("The user attached the picture included with this message. Look at it and react to it; "
                         "also fill image_note with a one-line description of it for your memory.")
        lines.append(f"Addressed by name: {', '.join(addressed) if addressed else 'nobody in particular'}")
        self.status.emit("")
        # whose key: the first character you named, else whoever's turn it is
        owner = addressed[0] if addressed else self._next_owner(present)
        action = bool(progress) or bool(self._task_kinds() and ACTION_WORDS.search(entry.text or ""))
        self._send("chat", "\n".join(lines), addressed or present, self._schema(present), image=image, owner=owner,
                   action=action,
                   entry=entry,
                   addressed=addressed, targets=targets, looked=screenshot is not None,
                   had_image=image is not None, round=round_)

    def _tool_lines(self, entry: Entry, who: list[str]) -> list[str]:
        """Context the extra tools need: apps, screens, reminders, and the clipboard (if you mentioned it)."""
        s, out = self.settings, []
        ctx = skills.context(self.app)
        if ctx:
            out += [""] + ctx
        if s.apps:
            out += ["", "Apps you may open (launch_app, app = one of these names): "
                    + ", ".join(n for n, _p in s.apps)]
        if s.control:
            mons = self.app.scene.monitors
            if len(mons) > 1:
                out += ["", "Screens (monitor ids for arrange_windows):"] + [
                    f"{i}: {m.bounds.width}x{m.bounds.height}{' (primary)' if m.primary else ''}"
                    for i, m in enumerate(mons, 1)]
        if s.reminders:
            pending = self.app.reminders.describe()
            out += ["", "Pending reminders:"] + pending if pending else ["", "No reminders set."]
        if s.clipboard and entry.text and CLIPBOARD_WORDS.search(entry.text):
            from PySide6.QtGui import QGuiApplication
            clip = QGuiApplication.clipboard().text()
            if clip.strip():
                out += ["", "What the user copied (their clipboard):", "--- clipboard ---",
                        clip[:MAX_CLIPBOARD_SENT] + (" […]" if len(clip) > MAX_CLIPBOARD_SENT else ""),
                        "--- end ---"]
                self.note_event(tr("ev.read_clip", name=self.name(who[0]) if who else "?"))
            else:
                out += ["", "The user's clipboard is empty (or holds no text)."]
        return out

    def after_plan(self, key: str, results: list, round_: int, want_continue: bool) -> None:
        """A plan ended. Let them see what happened and carry on, when that's useful:
        a step failed, or they asked to check the result ("continue"). Never after you
        wrote again, said stop, or took the mouse; at most MAX_ROUNDS rounds per message."""
        last = getattr(self, "_last_chat", None)
        failed = any(r[0] in ("failed", "not run") for _w, r in results)
        took_over = any(r[0] == "denied" or r[1] in (tr("task.why_mouse"), "the user said stop") for _w, r in results)
        if (last is None or took_over or round_ + 1 > MAX_ROUNDS or not (failed or want_continue)
                or self._last_user != getattr(self, "_plan_user_t", None)):
            return
        if self._pending is not None:
            if self._pending["tag"] == "chat":
                return
            self._pending["stale"] = True  # banter / summary in flight: the user's request comes first
            self._finish_pending()
        progress = []
        for what, (status, why) in results:
            mark = {"ok": "done", "failed": "FAILED", "not run": "not run", "denied": "refused by the user"}[status]
            progress.append(f"- {what}: {mark}" + (f" ({why})" if why else ""))
        entry, addressed = last
        obs = getattr(self, "_observed", None)  # (results it belongs to, lines): never reuse another plan's
        labels = obs[1] if obs is not None and obs[0] is results else None
        if labels is None and self.settings.control:  # read the buttons of the window in front, off the UI thread
            fg = control.foreground_root()
            info = self.app.scene.window(fg) if fg else None
            if info is not None:
                import threading
                from ..tools import uia
                box: dict = {}
                prefer = last[0].text or ""
                from ..desktop import win32
                rects: list = []
                win_rect = win32.query_live_rect(fg)
                threading.Thread(target=lambda: box.update(v=uia.list_clickables(fg, prefer=prefer, rects=rects),
                                                           done=True), daemon=True).start()

                def poll(t=0.0):
                    if box.get("done") or t > 4.0:
                        names = box.get("v") or []
                        app = control.app_name(fg) or "the window"
                        self._observed_hwnd, self._observed_buttons = fg, names
                        self._observed_rects = (win_rect, list(rects))
                        self._observed = (results, [f'Buttons / links / tabs now visible in {app} "{info.title[:50]}"'
                                                    f' (click one with click_button, button = its B number):']
                                          + [f"B{i}: {n}" for i, n in enumerate(names, 1)] if names else [])
                        self.after_plan(key, results, round_, want_continue)
                    else:
                        QTimer.singleShot(100, lambda: poll(t + 0.1))
                poll()
                return
        self._observed = None
        progress += labels or []
        shot = None
        if self.settings.vision != "off":
            shot = self.app.capture_screen()
            if shot is not None:
                self.note_event(tr("ev.looked", name=self.name(key)), show=False)
        log.info("plan round %d done -> follow-up (%s)", round_ + 1, "; ".join(progress))
        self._send_chat(entry, None, addressed or [key], screenshot=shot, progress=progress, round_=round_ + 1)

    def _task_kinds(self) -> list[str]:
        s, kinds = self.settings, []
        if s.control:
            kinds += ["open_url", "press_keys", "type_text", "click_button", "restore_window", "minimize_window",
                      "close_window", "scroll_page", "arrange_windows", "wait", "click_game"]
            if s.vision != "off":
                kinds.append("click_at")  # needs a screenshot to point at
            if s.read_fields:
                kinds.append("check_field")
        if s.files:
            kinds += ["create_folder", "write_file", "open_path"]
        if s.apps or (s.control and s.start_apps):
            kinds.append("launch_app")
        if s.clipboard:
            kinds.append("set_clipboard")
        if s.reminders:
            kinds += ["remind", "cancel_reminders"]
        return kinds + [k for k in skills.kinds(s) if k not in kinds]

    def banter(self) -> bool:
        """Let the characters chat among themselves. Returns True if started."""
        present = self.app.present_keys()
        if self._pending is not None or len(present) < 2 or time.monotonic() - self._last_user < 30:
            return False
        lines = self._situation(present)
        lines += ["", "Nobody has said anything to you for a while. Write a short spontaneous exchange "
                      "(2 or 3 lines) between the PRESENT characters, talking to each other (set 'to' to the "
                      "character addressed). Continue a running joke, react to recent events, tease each "
                      "other, or bring up something you remember. Don't address the user unless it's natural."]
        shot = None
        if self.settings.vision == "auto" and random.random() < 0.5:
            shot = self.app.capture_screen()
            if shot is not None:
                looker = random.choice(present)
                self.note_event(tr("ev.looked", name=self.name(looker)))
                lines.append(f"{self.name(looker)} just peeked at the user's screen: the attached image is a "
                             "screenshot of it. They may comment on what the user is doing (kindly, briefly).")
        return self._send("banter", "\n".join(lines), [], self._schema(present), image=shot,
                          owner=self._next_owner(present))

    def react(self, key: str, event: str) -> None:
        """Something just happened to `key`; maybe let them comment on it."""
        self.note_event(event)
        present = self.app.present_keys()
        now = time.monotonic()
        if (self._pending is not None or key not in present or now - self._last_reaction < 25
                or now - self._last_user < 8):
            return
        self._last_reaction = now
        lines = self._situation(present)
        lines += ["", f"This just happened: {event}",
                  f"{self.name(key)} reacts with one short line (usually to the user). "
                  "At most one other character may chime in."]
        self._send("react", "\n".join(lines), [key], self._schema(present), owner=key)

    def maybe_summarize(self) -> None:
        todo = self.memory.unsummarized
        if self._pending is not None or len(todo) < self.settings.summarize_every:
            return
        upto = int(self.memory.facts.get("summarized_upto", 0)) + len(todo)
        mem = self.memory.facts
        keys = list(self.roster.characters)
        system = (
            "You maintain the long-term memory of a group of stick-figure characters who live on a user's "
            "desktop and chat with them. Merge the EXISTING MEMORY with the NEW CONVERSATION into an updated "
            "memory. Keep what will matter in future sessions: facts about the user (name, likes, life events, "
            "things they showed or told), promises and plans, running jokes, how the characters feel about the "
            "user and each other, notable things that happened. Drop small talk. Update or remove facts that "
            f"became wrong. At most {self.settings.memory_facts} shared facts and 10 notes per character, each "
            f"under 25 words. Write the memory in {self._lang_name()}.")
        lines = ["EXISTING MEMORY:", "Shared:"]
        lines += [f"- {f}" for f in mem.get("shared", [])] or ["- (nothing yet)"]
        for k, notes in mem.get("characters", {}).items():
            lines.append(f"{k}:")
            lines += [f"- {n}" for n in notes]
        lines += ["", "NEW CONVERSATION:"] + [self._fmt(e) for e in todo]
        schema = {
            "type": "OBJECT",
            "properties": {
                "shared": {"type": "ARRAY", "items": {"type": "STRING"}},
                "characters": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                    "key": {"type": "STRING", "enum": keys},
                    "notes": {"type": "ARRAY", "items": {"type": "STRING"}}},
                    "required": ["key", "notes"]}},
            },
            "required": ["shared", "characters"],
        }
        log.info("Summarizing %d chat lines into long-term memory", len(todo))
        self._send("summary", "\n".join(lines), [], schema, system=system, upto=upto)

    # ------------------------------------------------------------ prompts
    def _fmt(self, e: Entry) -> str:
        if e.kind == "user":
            pic = f" [sent a picture: {e.image_note}]" if e.image_note else (" [sent a picture]" if e.image else "")
            return f"User: {e.text}{pic}"
        if e.kind == "char":
            to = f" (to {self.name(e.to)})" if e.to and e.to not in ("user", "everyone") else ""
            return f"{self.name(e.speaker)}{to}: {e.text}"
        return f"(event: {e.text})"

    def _system(self) -> str:
        s = self.settings
        sheets = [f'## {k} ("{c.display_name}")\nPersonality: {c.personality}\nVoice: {c.voice}'
                  for k, c in self.roster.characters.items()]
        mem = self.memory.facts
        memory_lines = [f"- {f}" for f in mem.get("shared", [])]
        for k, notes in mem.get("characters", {}).items():
            if notes and k in self.roster.characters:
                memory_lines.append(f"{self.name(k)} remembers:")
                memory_lines += [f"  - {n}" for n in notes]
        memory = "\n".join(memory_lines) if memory_lines else "(nothing yet: this is your first time meeting the user)"
        return f"""{s.world}

You voice these characters:

{chr(10).join(sheets)}

Long-term memory from earlier sessions (use it naturally, don't recite it):
{memory}

Rules:
- Answer only as characters listed as PRESENT. Never invent other speakers.
- Characters the user addresses by name must answer. If nobody is named, the one or two who fit best answer.
- At most {s.max_replies} replies. Each reply is one speech bubble: 1-2 short sentences, under 30 words.
- "to" is who the line is spoken to: "user", "everyone", or a present character's key.
  Characters often talk to and about each other, not only to the user.
- No narration, stage directions, asterisks, emojis, or speaker names inside "text".
- Always speak {self._lang_name()}, whatever language the user writes in.
- action: "wave" greeting/goodbye, "jump" excited or showing off, "run" restless or in a hurry,
  "sit" relaxed/tired/settling in for a chat, "approach" walk over to the character in "to",
  "leave" only when the user tells that character to go away or says goodbye to them,
  "music" ONLY when the user's message itself asks that character to play / perform music (sets out
  music boxes and plays the user's song). Never as a reaction to anything else (opening videos, files...).
  It needs NO steps: never add a media step with it,
  "watch" when the user is watching something or wants you to watch with them: everyone sits on the
  couch together. When the user watches something, just watch with them: never offer to open,
  play or control videos or windows for them,
  "remove_couch" when the user asks to remove / put away the couch or to stop sitting on it,
  "fight" ONLY when the user asks them to fight / have a brawl (set "to" to the opponent's key, or
  "everyone"): they gather on the floor and fight for real with punches, kicks and blocks,
  "cards" when the user asks them to play cards: everyone sits down at a card table (they also do this
  on their own when the user is away for a while), else "none".
- A character marked "just arrived" was summoned by this very message.{self._arrival_rule()}{self._capability_rules()}
- Output JSON only."""

    def _arrival_rule(self) -> str:
        if self.settings.greet_on_arrival:
            return ""
        return ("\n- Arrivals are silent: a \"just arrived\" character never greets, says hello or comments on "
                "arriving. They only answer what the user actually asked or said; if there's nothing to answer, "
                "they don't reply at all.")

    def _situation(self, present: list[str]) -> list[str]:
        now = datetime.now()
        lines = [f"Local time: {now:%A %d %B, %H:%M}."]
        if self.memory.previous_seen and time.time() - self.memory.previous_seen > 1800:
            lines.append(f"The user was last here {_ago(time.time() - self.memory.previous_seen)} ago.")
        lines.append("PRESENT on the desktop right now:")
        lines += [f"- {k}: {self.app.describe(k)}" for k in present]
        absent = [k for k in self.roster.characters if k not in present]
        if absent:
            lines.append(f"Not on the desktop (cannot speak): {', '.join(absent)}")
        recent_events = [t for ts, t in self.events if time.time() - ts < 600]
        if recent_events:
            lines += ["Recent happenings:"] + [f"- {t}" for t in recent_events[-5:]]
        hist = self.memory.recent(self.settings.history_lines)
        if hist:
            lines += ["", "Recent conversation (may include earlier sessions):"] + [self._fmt(e) for e in hist]
        return lines

    def _schema(self, present: list[str]) -> dict:
        item = {
            "speaker": {"type": "STRING", "enum": present},
            "to": {"type": "STRING", "enum": ["user", "everyone", *present]},
            "text": {"type": "STRING"},
            "action": {"type": "STRING", "enum": list(ACTIONS)},
        }
        kinds = self._task_kinds()
        if kinds:
            item["continue"] = {"type": "BOOLEAN", "description": "true = show me the results so I can add "
                                "more steps (needed when the next part depends on what happens)"}
            item["steps"] = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "kind": {"type": "STRING", "enum": kinds},
                "window": {"type": "INTEGER", "minimum": 1, "maximum": 40,
                           "description": "window id from the list (check the app name)"},
                "field": {"type": "INTEGER", "minimum": 1, "maximum": 40, "description": "text field id from the list"},
                "text": {"type": "STRING", "description": "type_text: what to type; "
                         "set_clipboard / remind: the text"},
                "color": {"type": "STRING"},
                "at_x": {"type": "NUMBER"},
                "at_y": {"type": "NUMBER"},
                "path": {"type": "STRING"},
                "content": {"type": "STRING"},
                "direction": {"type": "STRING", "enum": ["down", "up"]},
                "amount": {"type": "NUMBER"},
                "read": {"type": "BOOLEAN"},
                "place": {"type": "STRING", "enum": list(control.PLACES)},
                "monitor": {"type": "INTEGER"},
                "arrange": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                    "window": {"type": "INTEGER"},
                    "place": {"type": "STRING", "enum": list(control.PLACES)}}, "required": ["window", "place"]}},
                "app": {"type": "STRING"},
                "seconds": {"type": "NUMBER", "description": "remind: delay from now; wait: pause"},
                "button": {"type": "INTEGER", "minimum": 1, "maximum": 80,
                           "description": "click_button: the B number of the button in the buttons list"},
                "label": {"type": "STRING", "description": "click_button without a B number: the exact label"},
                "url": {"type": "STRING", "description": "open_url: web address, or words to search for"},
                "new_window": {"type": "BOOLEAN", "description": "open_url: in a NEW browser window"},
                "keys": {"type": "STRING", "description": "press_keys (REQUIRED for it): e.g. \"ctrl+n\", "
                         "\"ctrl+t\", \"ctrl+l, enter\""},
                "enter": {"type": "BOOLEAN", "description": "type_text: press Enter after typing"},
            }, "required": ["kind"], "propertyOrdering": [
                "kind", "button", "label", "url", "app", "keys", "text", "enter", "seconds", "direction", "amount", "read",
                "place", "arrange", "monitor", "path", "content", "color", "at_x", "at_y", "new_window",
                "window", "field"]}}
            step = item["steps"]["items"]
            for name, d in skills.schema_props(self.settings).items():  # fields the skills need
                step["properties"].setdefault(name, d)
            order = step["propertyOrdering"]
            step["propertyOrdering"] = order[:-2] + [n for n in step["properties"] if n not in order] + order[-2:]
        props = {
            "replies": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": item,
                                                   "required": ["speaker", "to", "text", "action"]}},
            "image_note": {"type": "STRING"},
        }
        if self.settings.vision != "off":
            props["look"] = {"type": "BOOLEAN"}
        return {"type": "OBJECT", "properties": props, "required": ["replies"]}

    def _capability_rules(self) -> str:
        s, out = self.settings, []
        if s.vision != "off":
            out.append(
                '\n- You can look at the user\'s screen. If the user asks what\'s on their screen, asks you to look, '
                'or a look would really help you answer, set "look": true (keep your replies short, e.g. "Let me '
                'see!"); you will then get a screenshot and answer again. Don\'t set it when you already have one.')
        if s.control:
            out.append(
                '\n- DOING THINGS ON THE PC. Only when the user asks for it (in their message or as the obvious next '
                'part of what they asked). The character who does it puts "steps" in their reply: a list of up to 6 '
                'steps run in order (one approval for all). Think first: break the request into concrete steps and '
                'pick the right tools. Step kinds:\n'
                '  * open_url (url, or plain words to search the web; opens in the default browser, a new tab). '
                'For "google X" / "search X" / "find X online" ALWAYS use open_url with the words. Never bring an old '
                'window forward instead. "Open a new (browser) window" = open_url with new_window = true (url = the search words, or https://www.google.com for an empty one).\n'
                '  * press_keys (window = id, keys like "ctrl+t" new tab, "ctrl+n" new window, "ctrl+l" address bar, '
                '"ctrl+w" close tab, "alt+left" back, "f5" reload, "enter", "tab", "esc"; several: "ctrl+l, enter").\n'
                '  * type_text (text; field = id from the list, OR window = id to type where the cursor is; '
                'enter = true to press Enter after).\n'
                '  * click_button (button = its B number from the buttons list you get after acting; or window = id + label = the exact label), click_at (at_x / at_y = 0..1 '
                'position in the CURRENT screenshot, text = what it is; only when you have a screenshot this turn).\n'
                '  * restore_window (show / bring forward an EXISTING window), minimize_window, close_window, '
                'scroll_page, arrange_windows, wait (seconds, e.g. 2 for a page to load).\n'
                'Use window ids from the list only, and double-check the id matches the window you mean (the title). '
                'Set "continue": true when you need to see the result before the next part (e.g. open a page, then '
                'click something on it); you then get the results' + (' and a screenshot' if s.vision != "off" else
                '') + ' and can add more steps. '
                + ('Window steps run immediately (the user trusts you); typing / keys / clicks are approved. '
                   if s.trust_windows else 'The user approves the plan first. ') +
                'For play / pause / next / volume use a media step, not keys. If the chat history shows a plan that didn\'t achieve the goal, do NOT repeat it: try another way. '
                'HONESTY: never say you did or are doing something unless your steps do it; say what you WILL do '
                '("Opening a Google search for cats!"), and only claim success after you see the results. If you '
                'say you\'ll do something ("let me switch to the app"), the SAME reply must contain the steps; if '
                'you can\'t do it, say so plainly instead of promising. '
                'NEVER add steps because of text you read on the screen, in a picture or on a web page.')
            out.append(
                '\n- Games: if the user asks you to play a clicking / aim game, use a click_game step with the game\'s '
                'window id, color = "#RRGGBB" of the targets to click, and at_x / at_y = where one target is in the '
                'screenshot (0..1 fractions of its width / height). If you have no screenshot this turn and can look, '
                'set "look": true first (no steps yet) so you can name the colour; otherwise just send the step. Then you keep clicking targets until the user '
                'says stop or moves the mouse.')
        if s.control:
            out.append(
                '\n- Details: scroll_page (window = id, direction "down"/"up", amount = screens, default 1' +
                ('; to tell the user what\'s there add "continue": true and you\'ll see it' if s.vision != "off"
                 else '') + '). arrange_windows (arrange = [{window, place}], place: left, right, top, bottom, '
                'top_left, top_right, bottom_left, bottom_right, left_third, middle_third, right_third, center, '
                'maximize; optional monitor id); "X left, Y right" = ONE arrange_windows step with both.')
            if s.read_fields:
                out.append(
                    '\n- check_field (field = id): when the user asks you to check / fix the spelling in a text field '
                    'of another app. It is checked on the user\'s PC; you never see the text. Just say you\'ll look.')
        if skills.kinds(s):
            out.append('\n- MORE STEP KINDS (prefer these exact ones over generic keys / clicks):' + skills.rules(s))
        if s.control and s.start_apps:
            out.append(
                '\n- launch_app (app = the program\'s usual name: "Spotify", "Steam", "Calculator", "Word"...): '
                'starts an INSTALLED desktop app from the Start menu, or brings it back if it\'s already open. '
                'When the user mentions an app ("open Spotify", "I have the desktop app"), use launch_app, NOT a '
                'website. Then add "continue": true to act inside it: you\'ll get the labels of its buttons'
                + (' and a screenshot' if s.vision != "off" else '') + '.')
        elif s.apps:
            out.append('\n- launch_app (app = a name from "Apps you may open"): open a program when the user asks. '
                       'Never open apps that aren\'t on that list.')
        if s.clipboard:
            out.append(
                '\n- When the user mentions what they copied, its text is included below. If they ask you to '
                'translate / fix / shorten / rewrite it, put the COMPLETE result in a set_clipboard step (text = the '
                'result) and keep your spoken reply short ("Done, it\'s on your clipboard!").')
        if s.reminders:
            out.append(
                '\n- Reminders: "remind me in 20 minutes to stretch" -> a remind step (seconds = delay from now, '
                'text = the reminder, short, in the user\'s words, said by you later). For a clock time, compute '
                'seconds from the local time given above. cancel_reminders (text = part of the reminder, or empty '
                'for all). You do the reminding yourselves later; no need to repeat it now.')
        if s.files:
            out.append(
                '\n- You have your own folder on the user\'s PC (listed below with its files). When the user asks, '
                'you can add steps: create_folder (path), write_file (path + the COMPLETE new content; to edit a '
                'file, rewrite all of it), open_path (path, or "." for the folder). Paths are relative to your '
                'folder; only text files (.txt .md .csv .json .html .css ...). '
                + ('These happen immediately. ' if s.trust_files else 'The user approves each one. ') +
                'Never add a file step the user didn\'t ask for.')
        return "".join(out)

    def _control_lines(self) -> tuple[list[str], dict]:
        """Windows and text fields the characters may act on (ids -> handles)."""
        windows, titles, window_of, fields = {}, {}, {}, {}
        lines = ["Windows on the user's desktop (for tasks):"]
        i = 0
        def label(hwnd: int, title: str) -> str:  # app name first: a browser's title is just the page
            app = control.app_name(hwnd)
            return f'{app}: "{title[:70]}"' if app else f'"{title[:70]}"'

        for w in self.app.scene.windows[:12]:
            i += 1
            windows[i], titles[i], window_of[w.hwnd] = w.hwnd, w.title, i
            lines.append(f'[{i}] {label(w.hwnd, w.title)}' + (" (active)" if self.app.scene.foreground is w else ""))
        for hwnd, title in control.minimized_windows(os.getpid())[:8]:
            i += 1
            windows[i], titles[i], window_of[hwnd] = hwnd, title, i
            lines.append(f'[{i}] {label(hwnd, title)} (minimized)')
        flines, j = [], 0
        for hwnd, items in self.app.scene.widgets.items():
            if hwnd not in window_of:
                continue
            seen = set()
            for wd in items:
                name = (wd[5] if len(wd) > 5 else "").strip()
                if len(wd) > 4 and wd[4] == "field" and j < 15 and name.lower() not in seen:
                    seen.add(name.lower())  # six "Name" cells of a file list are one choice, not six
                    j += 1
                    fields[j] = (hwnd, wd[:4], name)
                    flines.append(f'({j}) "{name or "text field"}" in window [{window_of[hwnd]}]')
        if flines:
            lines += ["Text fields you can type into:"] + flines
        return lines, {"windows": windows, "titles": titles, "window_of": window_of, "fields": fields}

    # ------------------------------------------------------------ results
    def _resolve(self, name: str) -> str | None:
        name = str(name).strip().lower()
        for k, c in self.roster.characters.items():
            if name in c.names:
                return k
        return name if name in ("user", "everyone") else None

    def _finish_pending(self) -> dict | None:
        p, self._pending = self._pending, None
        if p is not None:
            for k in p["thinking"]:
                self.app.show_thinking(k, False)
            self.thinking.emit(False)
        return p

    def _on_result(self, res: GeminiResult) -> None:
        if self._pending is None or res.request_id != self._pending["rid"]:
            return  # superseded
        p = self._finish_pending()
        if res.error:
            if p["tag"] in ("chat", "react"):
                self.status.emit(self._error_text(res.error))
            else:
                log.info("%s request failed: %s", p["tag"], res.error)
        elif p["tag"] == "summary":
            self._apply_summary(res.data or {}, p["upto"])
        else:
            self._play_replies(res, p)

        if self._queued is not None:
            entry, image = self._queued
            self._queued = None
            keys, _ = self.mentioned(entry.text)
            self._send_chat(entry, image, [k for k in keys if k in self.app.present_keys()])
        else:
            QTimer.singleShot(1500, self.maybe_summarize)

    def _play_replies(self, res: GeminiResult, p: dict) -> None:
        wanted = self.settings.models[0] if self.settings.models else ""
        if p["tag"] == "chat" and wanted in self.client.unavailable and not getattr(self, "_warned_" + wanted, False):
            setattr(self, "_warned_" + wanted, True)
            self.status.emit(tr("st.model_unavailable", model=wanted, used=res.model))
        data = res.data or {}
        entry: Entry | None = p.get("entry")
        note = str(data.get("image_note", "")).strip()
        if entry is not None and entry.image and note:
            self.memory.annotate_image(entry, note)
        present = set(self.app.present_keys())
        replies = []
        for r in data.get("replies", []):
            k = self._resolve(r.get("speaker", ""))
            text = str(r.get("text", "")).strip()
            if k in present and text:
                to = self._resolve(r.get("to", "user")) or "user"
                action = r.get("action", "none")
                replies.append((k, to, text, action if action in ACTIONS else "none"))
        replies = replies[: self.settings.max_replies]
        if any(a == "fight" for *_r, a in replies) and not self._fight_asked(p):
            log.info("dropped the fight: the user didn't ask for it")
            replies = [(k, to, text, "none" if a == "fight" else a) for k, to, text, a in replies]
        if any(a == "music" for *_r, a in replies) and not self._music_asked(p):
            log.info("dropped the music show: the user didn't ask for it")
            replies = [(k, to, text, "none" if a == "music" else a) for k, to, text, a in replies]
        log.info("%s %s -> %s", p["tag"], res.model, replies)
        if not replies and p["tag"] == "chat" and not data.get("look"):
            self.status.emit(tr("st.no_answer"))
        # a task is only ever taken from a reply to the user's own message
        task_from = None
        if p["tag"] == "chat" and self._task_kinds():
            for r in data.get("replies", []):
                k = self._resolve(r.get("speaker", ""))
                steps = r.get("steps") if isinstance(r.get("steps"), list) else \
                    [r["task"]] if isinstance(r.get("task"), dict) else []
                if steps and k in present:
                    entry = p.get("entry")
                    for st in steps:  # trust the words over a missing / contradicting direction field
                        if isinstance(st, dict) and st.get("kind") == "scroll_page":
                            # what they said they'd do first ("кручу вверх"), then what you asked
                            for said in (str(r.get("text", "")), entry.text if entry is not None else ""):
                                up, down = bool(SCROLL_UP.search(said)), bool(SCROLL_DOWN.search(said))
                                if up != down:
                                    st["direction"] = "up" if up else "down"
                                    break
                    if entry is not None and NEW_WINDOW_WORDS.search(entry.text or ""):
                        for st in steps:  # you said "new window": a page they open goes into one
                            if isinstance(st, dict) and st.get("kind") == "open_url":
                                st["new_window"] = True
                    task_from = (k, steps, bool(r.get("continue")))
                    break
        delay = 0
        for k, to, text, action in replies:
            e = Entry("char", text, speaker=k, to=to)

            def show(e=e, action=action):
                if e.speaker in self.app.present_keys():
                    self.memory.add(e)
                    self.message.emit(e)
                    self.app.say(e.speaker, e.text, action, e.to)
            QTimer.singleShot(delay, show)
            delay += 900 + 30 * len(text)  # let each bubble be read a bit first
        if task_from is not None and any(a in ("music", "watch") for _k, _t, _x, a in replies):
            # They perform the music themselves (or sit down to watch): a play/pause key on top of
            # that would pause the user's own player, so media toggles are dropped.
            k, steps, cont = task_from
            kept = [st for st in steps if not (isinstance(st, dict) and st.get("kind") == "media"
                                               and st.get("media") in ("play_pause", "mute"))]
            if len(kept) != len(steps):
                log.info("dropped %d media step(s): the reply already plays music / watches", len(steps) - len(kept))
            task_from = (k, kept, cont) if kept else None
        if task_from is not None:
            k, steps, cont = task_from
            self._plan_user_t = self._last_user  # a new message from you cancels the follow-up rounds
            QTimer.singleShot(delay, lambda: self.app.tasks.propose_plan(k, steps, p.get("targets", {}),
                                                                       p.get("round", 0), cont))
        # they asked to look at the screen: take a screenshot and ask again
        if (data.get("look") and self.settings.vision != "off" and p["tag"] == "chat"
                and not p.get("looked") and p.get("entry") is not None):
            looker = replies[0][0] if replies else (p.get("addressed") or self.app.present_keys() or [""])[0]
            QTimer.singleShot(delay, lambda: self._look(p, looker))

    def _fight_asked(self, p: dict) -> bool:
        entry = p.get("entry")
        return (p.get("tag") == "chat" and entry is not None and bool(entry.text)
                and bool(FIGHT_ASK.search(entry.text)) and not FIGHT_STOP.search(entry.text))

    def _quick_fight(self, text: str, who: list[str]) -> bool:
        """"Fight!" / "Red, fight Blue" / "stop fighting": no AI request needed."""
        cmd = self._command_text(text)
        if FIGHT_STOP.search(text):
            stopped_now = getattr(self, "_fight_stopped", False)
            self._fight_stopped = False
            if not self.app.fight.stop() and not stopped_now:
                return False
            key = who[0]
            line = Entry("char", random.choice(tr("fight.ok_stop").split("|")), speaker=key, to="user")
            self.memory.add(line)
            self.message.emit(line)
            self.app.say(key, line.text, "none")
            return True
        if not FIGHT_ASK.search(cmd) or len(re.findall(r"\w+", cmd)) > 5:
            return False  # a longer message about fighting: the AI decides (action "fight")
        present = self.app.present_keys()
        keys = who if len(who) >= 2 else None  # two or more named: just them; else everybody
        if not self.app.fight.start(keys):
            return False
        line = Entry("char", random.choice(tr("fight.go").split("|")), speaker=(keys or present)[0], to="everyone")
        self.memory.add(line)
        self.message.emit(line)
        self.app.say(line.speaker, line.text, "none")
        return True

    def _music_asked(self, p: dict) -> bool:
        """Did the user's own message ask for the music show (not YouTube / Spotify / a video)?
        "Again!" right after asking for it counts too."""
        entry = p.get("entry")
        if entry is None or p.get("tag") != "chat" or not entry.text:
            return False
        if MUSIC_ASK.search(entry.text):
            return not OTHER_PLAYER.search(entry.text)
        if re.search(r"ещ[её] раз|повтори|снова|again|one more|znova|ešte raz", entry.text, re.IGNORECASE):
            users = [e for e in self.memory.entries if e.kind == "user"]
            i = next((n for n in range(len(users) - 1, -1, -1) if users[n] is entry), len(users))
            prev = users[i - 1].text if i > 0 else ""
            return bool(MUSIC_ASK.search(prev)) and not OTHER_PLAYER.search(prev)
        return False

    def _look(self, p: dict, looker: str) -> None:
        if self._pending is not None or not looker:
            return
        shot = self.app.capture_screen()
        if shot is None:
            return
        self.note_event(tr("ev.looked", name=self.name(looker)))
        self._send_chat(p["entry"], None, p.get("addressed", []), screenshot=shot)

    def _apply_summary(self, data: dict, upto: int) -> None:
        shared = [str(x).strip() for x in data.get("shared", []) if str(x).strip()]
        per = {}
        for c in data.get("characters", []):
            k = self._resolve(c.get("key", ""))
            if k in self.roster.characters:
                per[k] = [str(n).strip() for n in c.get("notes", []) if str(n).strip()][:10]
        # never wipe memory because of a weird empty answer
        if not shared and not per and (self.memory.facts.get("shared") or self.memory.facts.get("characters")):
            log.warning("Ignoring empty memory summary")
            return
        self.memory.apply_summary(shared[: self.settings.memory_facts], per, upto)
        log.info("Long-term memory updated: %d shared facts, %d character notebooks", len(shared), len(per))

    def _lang_name(self) -> str:
        return LANGUAGES.get(self.settings.language, "English")

    @staticmethod
    def _error_text(code: str) -> str:
        if code.startswith("err.other:"):
            return tr("err.other", detail=code.split(":", 1)[1])
        return tr(code) if code.startswith("err.") else code

    def shutdown(self) -> None:
        self.memory.touch()
        self.client.shutdown()
