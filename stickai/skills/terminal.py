"""Terminal: their OWN Windows Terminal window ("StickAI terminal"), folders and Git.

Rules (agreed with the user):
* read-only commands run at once: cd, dir / ls, pwd, cls, git status / log / diff /
  branch / show / remote / fetch;
* commands that change things need Allow: git pull / checkout / switch / add /
  commit / push / stash, mkdir;
* anything else is refused, and so is chaining or redirection (; & | > < ` $ ...).

Commands are typed into their own terminal window only (never into one of yours,
which may be running something). The output is read back (UI Automation) so the
model can tell you what happened.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time

from ..i18n import tr
from ..tools import control
from .base import Action, Param, Skill, Unusable, background

TITLE = "StickAI terminal"
READ_GIT = {"status", "log", "diff", "branch", "show", "remote", "fetch", "rev-parse", "describe", "shortlog"}
CHANGE_GIT = {"pull", "checkout", "switch", "add", "commit", "push", "stash", "init", "clone", "tag"}
READ_CMDS = {"cd", "dir", "ls", "pwd", "cls", "clear", "chdir", "sl", "gl", "gci", "tree"}
CHANGE_CMDS = {"mkdir", "md"}
FORBIDDEN = re.compile(r"[;&|<>`$\n\r]|\(\s*\)")
GIT_DANGER = re.compile(r"(?:^|\s)(?:--force\b|-f\b|--force-with-lease|--hard\b|--delete\b|-D\b|--mirror\b|--prune\b)")
OUTPUT_TIMEOUT = 25.0
MAX_REPORT = 2500


def classify(command: str) -> str:
    """'read' | 'change'; raises Unusable with the reason otherwise."""
    cmd = " ".join(command.strip().split())
    if not cmd:
        raise Unusable("missing command")
    if FORBIDDEN.search(cmd):
        raise Unusable("no chaining, pipes, redirection or $variables: one plain command per step")
    try:
        parts = shlex.split(cmd, posix=False)
    except ValueError:
        raise Unusable("unbalanced quotes")
    head = parts[0].lower()
    if head == "git":
        sub = parts[1].lower() if len(parts) > 1 else ""
        if GIT_DANGER.search(cmd):
            raise Unusable("forced / destructive git options are not allowed")
        if sub == "stash" and len(parts) > 2 and parts[2].lower() == "list":
            return "read"
        if sub in READ_GIT:
            return "read"
        if sub in CHANGE_GIT:
            return "change"
        raise Unusable(f"git {sub} is not allowed (allowed: {', '.join(sorted(READ_GIT | CHANGE_GIT))})")
    if head in READ_CMDS:
        return "read"
    if head in CHANGE_CMDS:
        return "change"
    raise Unusable(f"{head!r} is not allowed in the terminal: only cd, dir, git and mkdir")


def find_terminal(app) -> int | None:
    """Their own terminal window (by its fixed title), open or minimized."""
    wins = [(w.hwnd, w.title) for w in app.scene.windows] + control.minimized_windows(os.getpid())
    for h, t in wins:
        if t == TITLE and control.app_name(h) == "Terminal":
            return h
    return None


def buffer_text(hwnd: int) -> str:
    from ..tools import uia
    UIA, auto = uia._auto()
    el = auto.ElementFromHandle(hwnd)
    tc = el.FindFirst(UIA.TreeScope_Descendants, auto.CreatePropertyCondition(UIA.UIA_ClassNamePropertyId,
                                                                               "TermControl"))
    if not tc:
        return ""
    pat = tc.GetCurrentPattern(UIA.UIA_TextPatternId).QueryInterface(UIA.IUIAutomationTextPattern)
    return pat.DocumentRange.GetText(-1) or ""


PROMPT = re.compile(r"^(?:PS )?([A-Za-z]:\\[^>]*)>\s*$")


def last_prompt_folder(text: str) -> str | None:
    for line in reversed([ln.rstrip() for ln in text.splitlines() if ln.strip()]):
        m = PROMPT.match(line)
        if m:
            return m.group(1)
    return None


class TerminalSkill(Skill):
    name = "terminal"
    KINDS = ("terminal",)

    def __init__(self):
        self.folder: str | None = None  # where their terminal is, as last seen

    def actions(self, settings):
        return [Action(
            "terminal", "run ONE command in your own terminal window (opened if needed) and read its output. "
                        "Allowed: cd <folder>, dir, git status/log/diff/branch/show/fetch (run at once); git pull/"
                        "checkout/switch/add/commit/push/stash, mkdir (the user approves). folder = where to run it "
                        "(a full path, or a folder name: it's found for you). Nothing else.",
            {"command": Param("STRING", "the command, e.g. 'git status' or 'git commit -m \"fix typo\"'"),
             "folder": Param("STRING", "optional: folder to run it in, full path or name like 'StickAI'")},
            ('"show git status of StickAI" -> {"kind":"terminal","command":"git status","folder":"StickAI"}',
             '"open terminal in Downloads" -> {"kind":"terminal","command":"cd","folder":"downloads"}'))]

    def context(self, app):
        if find_terminal(app) is None:
            return ["Your terminal: not open (it opens when you run a command)."]
        return [f"Your terminal is open" + (f" in {self.folder}" if self.folder else "") + "."]

    def resolve(self, runner, step, targets):
        command = " ".join(str(step.get("command") or step.get("text") or "").split())
        folder = str(step.get("folder") or "").strip().strip('"')
        head, _, arg = command.partition(" ")
        if head.lower() in ("cd", "chdir", "sl", "set-location") and arg and not folder:
            folder, command = arg.strip().strip('"'), "cd"  # "cd StickAI": the folder is found by name
        if not command and folder:
            command = "cd"
        level = classify(command) if command != "cd" else "read"
        shown = command + (f"  ({folder})" if folder else "")
        return {"kind": "terminal", "skill": self.name, "command": command, "folder": folder, "level": level,
                "trusted": level == "read", "what": tr("sk.term", cmd=shown)}

    # -------------------------------------------------------------------- run
    def _folder_path(self, raw: str) -> str | None:
        from .explorer import find, known_folder
        if not raw:
            return None
        if os.path.isabs(raw) and os.path.isdir(raw):  # a bare name is never relative to OUR folder
            return os.path.normpath(raw)
        k = known_folder(raw)
        if k:
            return k
        hits = [p for p in find(raw, "folder") if os.path.isdir(p)]
        return hits[0] if hits else None

    def run(self, runner, s, spec):
        from ..tools.tasks import TaskFailed
        app = runner.app
        folder = None
        if spec["folder"]:
            box = background(lambda: self._folder_path(spec["folder"]))
            yield ("until", lambda: box.get("done", False), 8.0)
            folder = box.get("value")
            if folder is None:
                raise TaskFailed(tr("sk.not_found", query=spec["folder"]))
        hwnd = find_terminal(app)
        if hwnd is None:  # open their own window, already in the right folder
            start = folder or os.path.expanduser("~")
            try:
                subprocess.Popen(["wt", "-w", "stickai", "nt", "--title", TITLE, "--suppressApplicationTitle",
                                  "-d", start], creationflags=0x08000000)
            except OSError as e:
                raise TaskFailed(str(e))
            for _ in range(40):
                yield ("wait", 0.25)
                app.scene.refresh()
                hwnd = find_terminal(app)
                if hwnd is not None:
                    break
            if hwnd is None:
                raise TaskFailed(tr("sk.term_no_window"))
            yield ("wait", 1.2)  # the shell starting up
            self.folder = start
            if spec["command"] == "cd":
                spec["done"] = tr("sk.term_opened", folder=start)
                return
        control.restore_window(hwnd)
        yield ("wait", 0.35)
        if control.foreground_root() != hwnd:
            raise TaskFailed(tr("task.why_focus"))
        lines = []
        if folder and os.path.normcase(folder) != os.path.normcase(self.folder or ""):
            lines.append(f'cd "{folder}"')
        if spec["command"] != "cd":
            lines.append(spec["command"])
        if not lines:
            spec["done"] = tr("sk.term_opened", folder=self.folder or "")
            return
        before = len(buffer_text(hwnd))
        s.gesture, s.gesture_until = "type", s.time + 0.03 * sum(map(len, lines)) + 2.0
        for line in lines:
            control.press_combo(["esc"])  # clear anything half-typed on the prompt
            for ch in line:
                if control.foreground_root() != hwnd:  # you switched away: stop typing
                    raise TaskFailed(tr("task.why_focus"))
                control.type_char(ch)
                yield ("wait", 0.02)
            control.press_combo(["enter"])
            yield ("wait", 0.4)
        s.gesture = ""
        # wait for the command to finish: the prompt is back at the end and the text stopped changing
        deadline, last, stable = time.monotonic() + OUTPUT_TIMEOUT, "", 0
        text = ""
        while time.monotonic() < deadline:
            box = background(lambda: buffer_text(hwnd))
            yield ("until", lambda: box.get("done", False), 3.0)
            text = box.get("value") or ""
            stable = stable + 1 if text == last else 0
            last = text
            if stable >= 2 and last_prompt_folder(text) is not None:
                break
            yield ("wait", 0.4)
        self.folder = last_prompt_folder(text) or self.folder
        new = text[before:] if len(text) > before else text[-MAX_REPORT:]
        out = "\n".join(ln.rstrip() for ln in new.splitlines() if ln.strip())[-MAX_REPORT:]
        spec["report"] = f"terminal now in {self.folder}; output:\n{out}" if out else f"terminal now in {self.folder}"
        spec["followup"] = spec["command"] not in ("cd",) and not spec["command"].startswith(("cd ", "cls", "clear"))
        spec["done"] = tr("sk.term_ran", cmd=spec["command"], folder=self.folder or "")
