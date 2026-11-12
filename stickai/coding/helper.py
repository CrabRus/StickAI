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

import logging
import random
import re
import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject

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
VSCODE_RECENT_S = 180   # VS Code had focus within this many seconds -> "this code" means its code
MAX_CONTEXT_LINES = 40
MAX_HISTORY = 6         # question/answer pairs of a thread sent back with a new question
JOBS = ("explain", "ask", "comment", "diagnose")


class CodeHelper(QObject):
    def __init__(self, app: "StickApp", secrets_path):
        super().__init__()
        self.app = app
        self.bridge = Bridge(self)
        self.bridge.request.connect(self._on_request)
        self.bridge.clients_changed.connect(lambda n: log.info("VS Code windows connected: %d", n))
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

    def connected(self) -> bool:
        return bool(self.bridge.connected())

    # ------------------------------------------------------------ from the chat
    def wants(self, text: str) -> str | None:
        """Which job a chat message asks for ("explain this code"...), if VS Code was just in use."""
        if not self.settings.vscode or self.bridge.recently_focused(VSCODE_RECENT_S) is None:
            return None
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
        elif kind == "run_failed":   # asked from the chat, but VS Code had nothing to work on
            key = str(msg.get("speaker") or self.coder())
            self._say(key, tr(f"code.fail.{msg.get('reason', 'no_editor')}"))
        elif kind == "applied":      # the user accepted Yellow's comments
            key = str(msg.get("speaker") or self.coder())
            if key in self.app.roster.characters and self.app.world.get(key) is not None:
                self.app.act(key, "jump")

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
        for sid, job in list(self._jobs.items()):  # one job per VS Code window at a time
            if job["client"] is c:
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
            "code, say what you'd need. Keep it short unless the question needs depth. " + self.STYLE)
        parts = _code_block(msg, "The code in question") if str(msg.get("code", "")).strip() else \
            ["(No code is open or selected.)"]
        history = [h for h in (msg.get("history") or []) if isinstance(h, dict)][-MAX_HISTORY:]
        if history:
            parts += ["", "Earlier in this conversation:"]
            for h in history:
                parts += [f"User: {str(h.get('q', ''))[:2000]}", f"You: {str(h.get('a', ''))[:4000]}", ""]
        parts += ["", f"The user's question: {str(msg.get('question', ''))[:4000]}"]
        return system, "\n".join(parts)

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
    def _say(self, key: str, text: str) -> None:
        if key not in self.app.roster.characters:
            return
        self.app.summon(key)
        line = Entry("char", text, speaker=key, to="user")
        self.app.memory.add(line)
        self.app.director.message.emit(line)
        self.app.say(key, text, "none")

    @staticmethod
    def _error_text(code: str) -> str:
        if code.startswith("err.other:"):
            return tr("err.other", detail=code.split(":", 1)[1])
        return tr(code) if code.startswith("err.") else code


PROMPTS = {"explain": CodeHelper._explain_prompt, "ask": CodeHelper._ask_prompt,
           "comment": CodeHelper._comment_prompt, "diagnose": CodeHelper._diagnose_prompt}
UI_LABELS = ("title", "thinking", "explain_of", "whole_file", "lines", "stop", "waiting", "empty",
             "comment_of", "error_of", "question", "ask_send", "writing_comments",
             "accept", "reject", "applied", "rejected", "unsafe", "stale", "no_errors", "copy", "copied",
             "review", "no_changes")


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
