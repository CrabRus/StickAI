"""The code helper: one character (Yellow by default) helps with code in VS Code.

Jobs, all started from VS Code (or from the StickAI chat while VS Code is in use):
  explain   the selection / function under the cursor / file, streamed into the panel
  ask       a question about that code, in the panel's question box (a short thread)
  comment   docstrings + comments; the result is checked (only comments may change)
            and shown as a diff that the user accepts or rejects in VS Code
  diagnose  "what's this error?": the problems VS Code reports there, explained + a fix
Code is sent to the model only for such a request, never in the background.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import random
import re
import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer

from ..brain.characters import LANGUAGES
from ..brain.memory import Entry
from ..i18n import tr
from .bridge import Bridge, Client
from .comments import extract_code, only_comments_added
from .stream import TextStream

if TYPE_CHECKING:
    from ..app import StickApp

log = logging.getLogger("stickai.code")

# Chat phrases, routed here only while VS Code was just in use (checked in this order).
COMMENT_WORDS = re.compile(
    r"прокомментир|откомментир|добавь (?:к \w+ )?коммент|напиши (?:к \w+ )?коммент|задокументир|докстринг|"
    r"add (?:some )?comments|comment (?:this|the|it|my)|document (?:this|the|it)|docstring|"
    r"okomentuj|pridaj komentár|zdokumentuj", re.IGNORECASE)
ERROR_WORDS = re.compile(
    r"ошибк|не работает|не компилир|ругается|подчёркнут|подчеркнут|"
    r"\berror\b|doesn'?t work|not working|won'?t compile|red squiggl|"
    r"chyb|nefunguje", re.IGNORECASE)
EXPLAIN_WORDS = re.compile(
    r"(?<!\w)(?:объясни|поясни|разбери|растолкуй|что (?:делает|значит|тут|здесь)|как (?:это |оно )?работает|"
    r"explain|what does|what is this|how does (?:this|it) work|walk me through|"
    r"vysvetli|čo robí|ako (?:to )?funguje)", re.IGNORECASE)
CODE_WORDS = re.compile(
    r"код|функци|метод|класс|файл|выделен|строк|скрипт|это|этот|эту|здесь|тут|"
    r"code|function|method|class|file|selection|selected|line|script|this|here|"
    r"kód|funkci|metód|triedu|súbor|označen|riad|toto|tento|tu", re.IGNORECASE)
# Commands run in VS Code's terminal (reported by the extension with their exit code and output).
TEST_CMD = re.compile(r"(?<![\w-])(?:pytest|py\.test|unittest|nose2|tox|jest|vitest|mocha|ctest|"
                      r"(?:npm|yarn|pnpm|bun)(?: run)? test|cargo test|go test|dotnet test|mvn test|gradle\w* test|"
                      r"make (?:test|check))(?![\w-])", re.IGNORECASE)
INTERRUPTED = {130, -1073741510, 3221225786, 2}  # Ctrl+C / killed: not an error worth explaining
ERROR_REPEAT_S = 120    # the same error again within this: don't explain it twice
# "что в терминале?", "почему упало в терминале", "what's wrong in the terminal"
TERMINAL_WORDS = re.compile(r"терминал|terminal|termináli|консол\w*|console", re.IGNORECASE)
VSCODE_RECENT_S = 180   # VS Code had focus within this many seconds -> "this code" means its code
MAX_CONTEXT_LINES = 40
MAX_HISTORY = 6         # question/answer pairs of a thread sent back with a new question
JOBS = ("explain", "ask", "comment", "diagnose", "hint", "quiz")
SAVE_HINT_EVERY_S = 120   # a hint after saving at most this often
STUCK_FAILS, STUCK_WINDOW_S = 3, 600   # the same command failing this often -> "you seem stuck"
# the four reviewers: one line each, from their own angle
REVIEW_ROLES = {"blue": "edge cases and hidden bugs (empty input, None, off-by-one, errors not handled)",
                "red": "speed and efficiency (needless work, slow loops, big copies)",
                "green": "names and readability (unclear names, too long, confusing), with a light joke",
                "yellow": "the big picture: what it does and the one thing worth improving most"}


class CodeHelper(QObject):
    def __init__(self, app: "StickApp", secrets_path):
        super().__init__()
        self.app = app
        self.bridge = Bridge(self)
        self.bridge.request.connect(self._on_request)
        self.bridge.clients_changed.connect(self._clients_changed)
        self._warned_old: set[int] = set()
        self.stream = TextStream(secrets_path)
        self.stream.chunk.connect(self._on_chunk)
        self.stream.finished.connect(self._on_finished)
        self._jobs: dict[int, dict] = {}   # stream id -> job
        self.apply_settings()

    # ------------------------------------------------------------ settings
    @property
    def settings(self):
        return self.app.roster.settings

    def coder(self) -> str:
        chars = self.app.roster.characters
        key = self.settings.coder
        return key if key in chars else next(iter(chars), "")

    def apply_settings(self) -> None:
        if self.settings.vscode:
            key = self.coder()
            spec = self.app.roster.characters.get(key)
            self.bridge.welcome = {
                "name": spec.display_name if spec else "StickAI", "color": spec.color if spec else "#FEC503",
                "labels": {k: tr(f"code.ui.{k}") for k in UI_LABELS},
            }
            if self.bridge.start():
                self.bridge.broadcast_welcome()
        else:
            self.bridge.stop()

    def shutdown(self) -> None:
        self.stream.shutdown()
        self.bridge.stop()

    def _clients_changed(self, n: int) -> None:
        log.info("VS Code windows connected: %d", n)
        want = bundled_version()
        for c in self.bridge.connected():
            if c.id in self._warned_old or not want or _ver(c.version) >= _ver(want):
                continue
            self._warned_old.add(c.id)  # an old extension is still loaded: VS Code needs a reload
            log.warning("VS Code runs the StickAI extension %s, %s is installed", c.version or "<0.3.2", want)
            QTimer.singleShot(1500, lambda: self._say(self.coder(), tr("code.ext_outdated")))

    def connected(self) -> bool:
        return bool(self.bridge.connected())

    def coding_now(self) -> bool:
        """VS Code is the window in front right now (the "coding" mode)."""
        c = self.bridge.active()
        return c is not None and bool(c.context.get("focused"))

    # ------------------------------------------------------------ from the chat
    def wants(self, text: str) -> str | None:
        """Which job a chat message asks for ("explain this code"...), if VS Code was just in use."""
        if not self.settings.vscode or self.bridge.recently_focused(VSCODE_RECENT_S) is None:
            return None
        if TERMINAL_WORDS.search(text) and (ERROR_WORDS.search(text) or EXPLAIN_WORDS.search(text) or re.search(
                r"что (?:там|в|у)|почему|упал|глянь|посмотри|прочитай|what'?s (?:in|wrong|up)|why|check|look at|"
                r"čo je|prečo|pozri", text, re.IGNORECASE)):
            return "terminal"  # "что в терминале?" - not "открой терминал" (that's the terminal skill)
        if COMMENT_WORDS.search(text):
            return "comment"
        if ERROR_WORDS.search(text) and (EXPLAIN_WORDS.search(text) or re.search(
                r"что за|почему|откуда|как (?:исправ|почин)|what|why|how (?:to|do i) fix|čo je|prečo|ako oprav",
                text, re.IGNORECASE)):
            return "diagnose"
        if EXPLAIN_WORDS.search(text) and CODE_WORDS.search(text):
            return "explain"
        return None

    def run_from_chat(self, job: str, key: str) -> None:
        c = self.bridge.recently_focused(VSCODE_RECENT_S)
        if c is None:
            return
        self.bridge.send(c, {"type": "run", "action": job, "speaker": key})
        self._say(key, random.choice(tr(f"code.on_it.{job}").split("|")))

    # ------------------------------------------------------------ requests from VS Code
    def _on_request(self, c: Client, msg: dict) -> None:
        kind = msg.get("type")
        if kind in JOBS:
            self._start(c, kind, msg)
        elif kind == "cancel":
            for sid, job in list(self._jobs.items()):
                if job["client"] is c and job["id"] == msg.get("id"):
                    self.stream.cancel(sid)
        elif kind == "run_failed" and msg.get("reason") == "no_run":
            self._say(str(msg.get("speaker") or self.coder()), tr("code.fail.no_run"))
        elif kind == "run_failed":   # asked from the chat, but VS Code had nothing to work on
            key = str(msg.get("speaker") or self.coder())
            self._say(key, tr(f"code.fail.{msg.get('reason', 'no_editor')}"))
        elif kind == "run_end":      # a command finished in VS Code's terminal
            self._run_end(c, msg)
        elif kind == "saved":        # a file was saved: a quick look at what changed
            self._saved(c, msg)
        elif kind == "review":       # four characters, one line each
            self._review(c, msg)
        elif kind == "applied":      # the user accepted Yellow's comments
            key = str(msg.get("speaker") or self.coder())
            if key in self.app.roster.characters and self.app.world.get(key) is not None:
                self.app.act(key, "jump")

    # ------------------------------------------------------------ terminal runs
    def _run_end(self, c: Client, msg: dict) -> None:
        if not self.settings.vscode or not self.settings.code_watch:
            return
        code = msg.get("exit_code")
        command = str(msg.get("command", ""))[:300]
        if code is None or code in INTERRUPTED or not command.strip():
            return
        output = str(msg.get("output", ""))
        key = self.coder()
        if TEST_CMD.search(command):
            self._tests_done(key, code == 0, output, c, msg)
        elif code != 0:
            if self._stuck(c, key, command, msg, output):
                return
            self._explain_error(c, key, msg, output)

    def _stuck(self, c: Client, key: str, command: str, msg: dict, output: str) -> bool:
        """The same command failing again and again: offer a proper look instead of one more one-liner."""
        now = time.monotonic()
        fails = getattr(self, "_fails", {})
        times = [t for t in fails.get(command, []) if now - t < STUCK_WINDOW_S] + [now]
        fails[command] = times
        self._fails = fails
        if len(times) < STUCK_FAILS:
            return False
        fails[command] = []
        tail = "\n".join(output.strip().splitlines()[-25:])
        question = tr("code.stuck_q", command=command[:120]) + "\n\n" + tail
        on_click = lambda: self.bridge.send(c, {"type": "ask_about", "text": question})
        self._say(key, tr("code.stuck", n=len(times)), on_click=on_click)
        return True

    def _tests_done(self, key: str, ok: bool, output: str, c: Client, msg: dict) -> None:
        present = self.app.present_keys()
        if ok:
            m = re.search(r"(\d+) passed", output)
            text = random.choice(tr("code.tests_ok").split("|")).format(n=m.group(1) if m else "")
            for k in present:  # everybody's happy
                s = self.app.world.get(k)
                if s is not None:
                    s.jump()
                    if k != key:
                        s.wave(1.5)
            self._say(key, text.replace(" ()", ""))
            return
        failed = re.search(r"(\d+) failed", output)
        s = self.app.world.get(key)
        if s is not None:
            s.gesture, s.gesture_until = "facepalm", s.time + 2.5
        if msg.get("location"):  # a traceback points somewhere: explain the first failure
            self._explain_error(c, key, msg, output, tests=True)
        else:
            self._say(key, tr("code.tests_fail", n=failed.group(1) if failed else "?"))

    def _explain_error(self, c: Client, key: str, msg: dict, output: str, tests: bool = False) -> None:
        tail = output.strip().splitlines()[-40:]
        if not tail:
            return
        sig = re.sub(r"\d+", "#", " ".join(tail[-3:]))[:300]  # the same error, give or take numbers
        now = time.monotonic()
        last = getattr(self, "_last_errors", {})
        if now - last.get(sig, -1e9) < ERROR_REPEAT_S:
            return
        last[sig] = now
        self._last_errors = {k: v for k, v in last.items() if now - v < ERROR_REPEAT_S}
        if not self.stream.has_key(key):
            return
        loc = msg.get("location") or {}
        lang = LANGUAGES.get(self.settings.language, "English")
        spec = self.app.roster.characters.get(key)
        name = spec.display_name if spec else "Yellow"
        system = (
            f"You are {name}, a tiny programmer stick figure watching the user's terminal. A command just "
            f"{'ran the tests and some failed' if tests else 'failed'}. In {lang}, say in ONE or TWO short sentences "
            "(max ~160 characters) what went wrong and the most likely fix. "
            + ("Name the line (L42) from the code below. " if (msg.get("location") or {}).get("code")
               else "You don't see the code: NEVER invent line numbers or names that aren't in the output. ") +
            "Talk like a friend (in Russian «ты»). No greeting, no markdown, no code blocks, no quotes. "
            "If it's not really an error (e.g. a warning, a usage message), say so in a few words.")
        parts = [f"Command: {msg.get('command', '')}", f"Exit code: {msg.get('exit_code')}",
                 "Last output:", *tail]
        if loc.get("code"):
            start = int(loc.get("start", 1) or 1)
            numbered = "\n".join(f"L{start + i}: {ln}" for i, ln in enumerate(str(loc["code"]).split("\n")))
            parts += ["", f"Code around {loc.get('file', '?')}:{loc.get('line', '?')}:", numbered]
        self.app.summon(key)
        self.app.show_thinking(key, True)
        models = tuple(sorted(self.settings.models, key=lambda m: "lite" in m))
        sid = self.stream.start(models, system, "\n".join(parts), 0.2, owner=key)
        self._jobs[sid] = {"client": c, "id": None, "key": key, "kind": "run_error", "msg": msg, "text": [],
                           "t": time.monotonic(), "from_chat": False, "loc": loc}
        log.info("explaining a failed run: %s", str(msg.get("command", ""))[:80])

    def _error_hint_done(self, job: dict) -> None:
        text = " ".join("".join(job["text"]).split()).strip(" \"'«»")
        if not text:
            return
        if len(text) > 260:
            text = text[:257].rstrip() + "…"
        loc, c = job.get("loc") or {}, job["client"]
        on_click = None
        if loc.get("file") and loc.get("line"):
            on_click = lambda: self.bridge.send(c, {"type": "goto", "file": loc["file"], "line": loc["line"]})
        self._say(job["key"], text, on_click=on_click)

    # ------------------------------------------------------------ after saving
    def _saved(self, c: Client, msg: dict) -> None:
        if not self.settings.vscode or not self.settings.save_hints:
            return
        code = str(msg.get("code", ""))
        if not code.strip() or len(code) > 12000:
            return
        now = time.monotonic()
        if now - getattr(self, "_last_save_hint", -1e9) < SAVE_HINT_EVERY_S:
            return
        if any(j["kind"] in ("save_hint", "run_error") for j in self._jobs.values()):
            return
        key = self.coder()
        if not self.stream.has_key(key):
            return
        self._last_save_hint = now
        lang = LANGUAGES.get(self.settings.language, "English")
        system = (
            "You glance over a programmer's shoulder right after they saved a file. Look ONLY at the changed "
            "lines (marked with >). If there is a REAL problem there - a bug, an unhandled case, a wrong API use, "
            "a leftover debug print, a typo in a name, an issue the editor reports - reply in " + lang +
            " with ONE short sentence (max ~140 characters) that names the line (L42) and the fix. Talk like a "
            "friend (Russian: «ты»). If nothing is clearly wrong, reply exactly: OK. Never nitpick style, never "
            "praise, no greeting, no markdown.")
        start = int(msg.get("start_line", 1) or 1)
        changed = set(range(int(msg.get("changed_from", start) or start), int(msg.get("changed_to", start) or start) + 1))
        lines = [f"{'>' if start + i in changed else ' '} L{start + i}: {ln}" for i, ln in enumerate(code.split("\n"))]
        parts = [f"File: {msg.get('file', '?')} ({msg.get('language', '')})", *lines]
        diags = [d for d in (msg.get("diagnostics") or []) if isinstance(d, dict)][:6]
        if diags:
            parts += ["", "The editor reports:"] + [f"- L{d.get('line')}: {d.get('message', '')[:200]}" for d in diags]
        models = tuple(sorted(self.settings.models, key=lambda m: "lite" in m))
        sid = self.stream.start(models, system, "\n".join(parts), 0.2, owner=key)
        self._jobs[sid] = {"client": c, "id": None, "key": key, "kind": "save_hint", "msg": msg, "text": [],
                           "t": now, "from_chat": False, "loc": {"file": msg.get("path"), "line": 0}}

    def _save_hint_done(self, job: dict) -> None:
        text = " ".join("".join(job["text"]).split()).strip(" \"'«».")
        log.info("save hint for %s: %r", job["msg"].get("file"), text[:120])
        if not text or text.upper().startswith("OK") or len(text) < 8:
            return  # nothing worth saying: stay quiet
        m = re.search(r"\bL(\d{1,5})\b", text)
        if m:
            job["loc"]["line"] = int(m.group(1))
        self._error_hint_done(job)

    # ------------------------------------------------------------ review by four
    def _review(self, c: Client, msg: dict) -> None:
        code = str(msg.get("code", ""))
        if not code.strip():
            return
        chars = self.app.roster.characters
        roles = {k: r for k, r in REVIEW_ROLES.items() if k in chars} or {self.coder(): REVIEW_ROLES["yellow"]}
        key = self.coder()
        if not self.stream.has_key(key):
            self.bridge.send(c, {"type": "error", "id": msg.get("id"), "message": tr("err.no_key")})
            return
        for k in roles:
            self.app.summon(k)
            self.app.show_thinking(k, True)
        lang = LANGUAGES.get(self.settings.language, "English")
        names = {k: chars[k].display_name if k in chars else k for k in roles}
        system = (
            "Four tiny stick figures review a piece of code; each says ONE short sentence (max ~110 characters) "
            f"from their own angle, in {lang}, talking to the user as a friend (Russian: «ты»). Name the line "
            "(L12) when it helps. If a reviewer has nothing real to say from their angle, they say so briefly "
            "(\"по скорости всё ок\"). Reply with JSON only: {\"key\": \"sentence\", ...} with exactly these "
            "keys:\n" + "\n".join(f"- {k} ({names[k]}): {r}" for k, r in roles.items()))
        prompt = "\n".join(_code_block(msg, "The code"))
        models = tuple(sorted(self.settings.models, key=lambda m: "lite" in m))
        sid = self.stream.start(models, system, prompt, 0.5, owner=key)
        self._jobs[sid] = {"client": c, "id": msg.get("id"), "key": key, "kind": "review", "msg": msg, "text": [],
                           "t": time.monotonic(), "from_chat": False, "roles": list(roles)}

    def _review_done(self, job: dict) -> None:
        raw = "".join(job["text"])
        for k in job["roles"]:
            self.app.show_thinking(k, False)
        try:
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except ValueError:
            data = {}
        c = job["client"]
        start = int(job["msg"].get("start_line", 1) or 1)
        path_ = job["msg"].get("path")
        delay = 0
        for k in job["roles"]:
            text = " ".join(str(data.get(k, "")).split())
            if not text:
                continue
            m = re.search(r"\bL(\d{1,5})\b", text)
            line = int(m.group(1)) if m else start
            on_click = (lambda line=line: self.bridge.send(c, {"type": "goto", "file": path_, "line": line})) \
                if path_ else None
            QTimer.singleShot(delay, lambda k=k, text=text, oc=on_click: self._say(k, text, on_click=oc))
            delay += 1400

    def _start(self, c: Client, kind: str, msg: dict) -> None:
        req_id = msg.get("id")
        code = str(msg.get("code", ""))
        if kind != "ask" and not code.strip():
            self.bridge.send(c, {"type": "error", "id": req_id, "message": tr("code.ui.empty")})
            return
        if kind == "ask" and not str(msg.get("question", "")).strip():
            return
        key = str(msg.get("speaker") or "") or self.coder()
        if key not in self.app.roster.characters:
            key = self.coder()
        for sid, job in list(self._jobs.items()):  # one panel job per VS Code window at a time
            if job["client"] is c and job["kind"] in JOBS:
                self.stream.cancel(sid)
        if not self.stream.has_key(key):
            self.bridge.send(c, {"type": "error", "id": req_id, "message": tr("err.no_key")})
            return
        self.app.summon(key)
        models = tuple(sorted(self.settings.models, key=lambda m: "lite" in m))  # the smarter model first
        system, prompt = PROMPTS[kind](self, key, msg)
        sid = self.stream.start(models, system, prompt, 0.2 if kind == "comment" else 0.4, owner=key)
        self._jobs[sid] = {"client": c, "id": req_id, "key": key, "kind": kind, "msg": msg,
                           "text": [], "t": time.monotonic(), "from_chat": bool(msg.get("speaker"))}
        self.bridge.send(c, {"type": "start", "id": req_id})
        self.app.show_thinking(key, True)
        self._laptop(key, True)
        log.info("%s: %s (%s, %d chars)", kind, msg.get("file", "?"), msg.get("language", "?"), len(code))

    def _on_chunk(self, sid: int, text: str) -> None:
        job = self._jobs.get(sid)
        if job is not None and job["kind"] in ("run_error", "save_hint", "review"):
            job["text"].append(text)  # shown at once when done (bubbles, not the panel)
            return
        if job is not None:
            job["text"].append(text)
            self.bridge.send(job["client"], {"type": "chunk", "id": job["id"], "text": text})

    def _on_finished(self, sid: int, err: str, model: str) -> None:
        job = self._jobs.pop(sid, None)
        if job is None:
            return
        key, c = job["key"], job["client"]
        self.app.show_thinking(key, False)
        self._laptop(key, False)
        if job["kind"] == "run_error":
            if not err:
                self._error_hint_done(job)
            return
        if job["kind"] == "save_hint":
            if not err:
                self._save_hint_done(job)
            return
        if job["kind"] == "review":
            if not err:
                self._review_done(job)
            else:
                self._say(key, self._error_text(err))
            return
        if err:
            text = self._error_text(err)
            self.bridge.send(c, {"type": "error", "id": job["id"], "message": text})
            self._say(key, text)
            return
        log.info("%s done in %.1f s with %s", job["kind"], time.monotonic() - job["t"], model)
        if job["kind"] == "comment" and job["text"]:
            original = str(job["msg"].get("code", ""))
            proposed = extract_code("".join(job["text"]))
            if proposed.strip() == original.strip():
                self.bridge.send(c, {"type": "error", "id": job["id"], "message": tr("code.ui.no_changes")})
                return
            safe = only_comments_added(original, proposed)
            if not safe:
                log.warning("comment proposal changes more than comments")
            self.bridge.send(c, {"type": "proposal", "id": job["id"], "code": proposed, "safe": safe,
                                 "speaker": key})
            if job["from_chat"]:
                self._say(key, tr("code.proposal_ready"))
            return
        self.bridge.send(c, {"type": "done", "id": job["id"], "model": model})

    # ------------------------------------------------------------ prompts
    def _persona(self, key: str) -> tuple[str, str]:
        spec = self.app.roster.characters.get(key)
        name = spec.display_name if spec else "Yellow"
        persona = f" Your personality: {spec.personality.strip()}" if spec and spec.personality.strip() else ""
        return (f"You are {name}, a small stick figure who lives on the user's desktop and is also a good, "
                f"patient programmer.{persona}", LANGUAGES.get(self.settings.language, "English"))

    STYLE = ("Talk to the user informally, as a friend (in Russian use «ты», never «вы»). A touch of your "
             "personality is fine (one short phrase), but never start with a greeting or an interjection "
             "(no \"Hi\", \"Hey\", \"Привет\", \"Эй\"), no stories about yourself, no emojis and no closing "
             "questions: get to the point. Be accurate: check what the code really does before claiming it; if "
             "unsure, say so. In code blocks write plain code, never the Lnn: prefixes.")

    def _explain_prompt(self, key: str, msg: dict) -> tuple[str, str]:
        me, lang = self._persona(key)
        system = (
            f"{me} The user selected code in VS Code and wants to understand it. "
            f"Write in {lang}; keep identifiers, code and technical terms as they are.\n"
            "Format: GitHub Markdown, rendered in a narrow side panel.\n"
            "1. Start with one or two plain sentences: what this code is for, as a whole.\n"
            "2. A short heading (in the user's language) and an explanation of HOW it works: go through "
            "the important parts in order, naming them in `backticks` and line numbers (L12) when it helps. "
            "Explain the why, not just the what; skip trivial lines.\n"
            "3. Only if there is something real: a short heading and a few bullets about pitfalls, bugs, "
            "edge cases or a clearly better way. Never invent problems.\n"
            "Be concise: about 120-250 words for a short snippet, more only for long code. "
            "Don't repeat the code back; at most a tiny fragment. " + self.STYLE)
        return system, "\n".join(_code_block(msg, "Code to explain"))

    def _ask_prompt(self, key: str, msg: dict) -> tuple[str, str]:
        me, lang = self._persona(key)
        system = (
            f"{me} The user is working in VS Code and asks you a question, usually about the code shown. "
            f"Answer in {lang}, in GitHub Markdown for a narrow side panel. Answer exactly what was asked, "
            "concretely, using the code (name things in `backticks`, line numbers as L12). If a change is "
            "needed, show only the changed part in a code block. If the question can't be answered from the "
            "code, say what you'd need. Keep it short unless the question needs depth. If they ask what you can do or "
            "for your commands, tell them to type /help in this box. " + self.STYLE)
        parts = _code_block(msg, "The code in question") if str(msg.get("code", "")).strip() else \
            ["(No code is open or selected.)"]
        history = [h for h in (msg.get("history") or []) if isinstance(h, dict)][-MAX_HISTORY:]
        if history:
            parts += ["", "Earlier in this conversation:"]
            for h in history:
                parts += [f"User: {str(h.get('q', ''))[:2000]}", f"You: {str(h.get('a', ''))[:4000]}", ""]
        parts += ["", f"The user's question: {str(msg.get('question', ''))[:4000]}"]
        return system, "\n".join(parts)

    def _hint_prompt(self, key: str, msg: dict) -> tuple[str, str]:
        """Study mode: help without giving the answer away. Level 1 = direction, 2 = where, 3 = almost."""
        me, lang = self._persona(key)
        level = max(1, min(3, int(msg.get("level", 1) or 1)))
        how = {1: "Give ONLY a direction: which idea or concept to think about. No code, no line-by-line fix.",
               2: "Point to WHERE the problem or the missing piece is (the line, L12) and what kind of change is "
                  "needed, still without writing the solution.",
               3: "Almost the solution: describe the exact change step by step; at most 1-2 lines of code, never "
                  "the whole thing."}[level]
        system = (
            f"{me} The user is a student working on this code (maybe an assignment) and asked for a HINT, "
            f"level {level} of 3. {how} Write in {lang}, GitHub Markdown, 2-5 short sentences. If the code "
            "already works and nothing is missing, say what to test next instead. " + self.STYLE)
        parts = _code_block(msg, "The code")
        if msg.get("question"):
            parts += ["", f"What the student is trying to do: {msg['question']}"]
        return system, "\n".join(parts)

    def _quiz_prompt(self, key: str, msg: dict) -> tuple[str, str]:
        """Check understanding: questions about the user's own code (answered in the panel's box)."""
        me, lang = self._persona(key)
        system = (
            f"{me} Check whether the user really understands their own code: ask exactly 3 short questions "
            f"about it, from easy to harder (what a line does, what happens on an edge case, why it's written "
            f"this way). Write in {lang}, as a numbered Markdown list, no answers. Then one line: they can answer "
            "in the box below and you'll check. " + self.STYLE)
        return system, "\n".join(_code_block(msg, "The code"))

    def _comment_prompt(self, key: str, msg: dict) -> tuple[str, str]:
        system = (
            "You add documentation comments to code. Reply with ONE fenced code block containing the complete "
            "given code with comments added, and nothing else: no text before or after.\n"
            "Rules:\n"
            "- NEVER change, reorder, reformat, rename or remove any code. Keep every code line byte-for-byte, "
            "with the exact same indentation (tabs stay tabs).\n"
            "- Give each function, method and class that has none a doc comment in the idiomatic style of the "
            "language: Python docstrings (first line a summary; Args/Returns/Raises only when they help), "
            "JSDoc/TSDoc, Javadoc, C# /// XML docs, Doxygen for C/C++, Go and Rust doc comments, etc.\n"
            "- Inline comments only where the logic is not obvious: explain WHY, not what the code literally "
            "does. Never comment trivial lines. Fewer, better comments.\n"
            "- Keep existing comments; fix one only if it is clearly wrong.\n"
            "- Comment language: the same human language as the existing comments in the code; if there are "
            "none, English.\n"
            "- The first line of the code may start inside a class or function: keep its indentation.")
        lang = str(msg.get("language", "")) or "text"
        parts = [f"File: {msg.get('file', 'untitled')} ({lang})", "", "```" + lang, str(msg.get("code", "")), "```"]
        return system, "\n".join(parts)

    def _diagnose_prompt(self, key: str, msg: dict) -> tuple[str, str]:
        me, lang = self._persona(key)
        system = (
            f"{me} VS Code reports a problem in the user's code and they want to understand it. "
            f"Write in {lang}, GitHub Markdown for a narrow side panel:\n"
            "1. What the message means, in plain words (one or two sentences).\n"
            "2. Why it happens HERE: point at the exact line (L12) and the cause.\n"
            "3. How to fix it: the corrected lines in a code block (only the lines that change, with a "
            "line or two around them), plus one sentence on why that fixes it. If there are several "
            "reasonable fixes, name the best one first.\n"
            "If the reported problem is a false alarm (e.g. a missing import the linter can't see), say so. "
            "Be concise. " + self.STYLE)
        parts = ["Problems reported by VS Code:"]
        for d in (msg.get("diagnostics") or [])[:8]:
            if isinstance(d, dict):
                src = f" [{d.get('source', '')}{' ' + str(d.get('code')) if d.get('code') else ''}]" \
                    if d.get("source") else ""
                parts.append(f"- L{d.get('line', '?')} {d.get('severity', 'error')}{src}: {str(d.get('message', ''))[:600]}")
        parts += [""] + _code_block(msg, "The code around it")
        return system, "\n".join(parts)

    def _laptop(self, key: str, on: bool) -> None:
        """Programmer mode: the laptop in his hands while he reads / writes code."""
        s = self.app.world.get(key)
        if s is None:
            return
        if on:
            s.talk(2.0)
            s.gesture, s.gesture_until = "laptop", s.time + 120.0
        elif s.gesture == "laptop":
            s.gesture_until = s.time + 1.5

    # ------------------------------------------------------------ talking
    def _say(self, key: str, text: str, on_click=None) -> None:
        if key not in self.app.roster.characters:
            return
        self.app.summon(key)
        line = Entry("char", text, speaker=key, to="user")
        self.app.memory.add(line)
        self.app.director.message.emit(line)
        # code hints are short and useful: a full bubble even with "indicator" bubbles
        self.app.say(key, text, "none", force_bubble=on_click is not None, on_click=on_click)

    @staticmethod
    def _error_text(code: str) -> str:
        if code.startswith("err.other:"):
            return tr("err.other", detail=code.split(":", 1)[1])
        return tr(code) if code.startswith("err.") else code


PROMPTS = {"explain": CodeHelper._explain_prompt, "ask": CodeHelper._ask_prompt,
           "hint": CodeHelper._hint_prompt, "quiz": CodeHelper._quiz_prompt,
           "comment": CodeHelper._comment_prompt, "diagnose": CodeHelper._diagnose_prompt}
UI_LABELS = ("title", "thinking", "explain_of", "whole_file", "lines", "stop", "waiting", "empty", "hint_of", "quiz_of",
             "comment_of", "error_of", "question", "ask_send", "writing_comments",
             "accept", "reject", "applied", "rejected", "unsafe", "stale", "no_errors", "copy", "copied",
             "review", "no_changes")


def bundled_version() -> str:
    """The extension version in this checkout (vscode/package.json)."""
    try:
        p = Path(__file__).resolve().parents[2] / "vscode" / "package.json"
        return str(json.loads(p.read_text(encoding="utf-8")).get("version", ""))
    except (OSError, ValueError):
        return ""


def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def _code_block(msg: dict, title: str) -> list[str]:
    """File line + context above/below + the code with line numbers."""
    lang = str(msg.get("language", "")) or "text"
    start = int(msg.get("start_line", 1) or 1)
    code_lines = str(msg.get("code", "")).split("\n")
    numbered = "\n".join(f"L{start + i}: {ln}" for i, ln in enumerate(code_lines))
    parts = [f"File: {msg.get('file', 'untitled')} ({lang})"]
    if msg.get("whole"):
        parts.append("The whole file.")
    else:
        parts.append(f"Lines {start}-{start + len(code_lines) - 1}.")
    if msg.get("truncated"):
        parts.append("(cut: the file is long, only part of it is included)")
    before = str(msg.get("before", "")).split("\n")[-MAX_CONTEXT_LINES:] if msg.get("before") else []
    after = str(msg.get("after", "")).split("\n")[:MAX_CONTEXT_LINES] if msg.get("after") else []
    if before:
        parts += ["", "Context above (for reference only):", "```" + lang, *before, "```"]
    parts += ["", f"{title} (line numbers are prefixed as Lnn:, not part of the code):",
              "```" + lang, numbered, "```"]
    if after:
        parts += ["", "Context below (for reference only):", "```" + lang, *after, "```"]
    return parts
