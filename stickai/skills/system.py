"""System: media keys and volume, and apps by NAME - close, force-close, hide, show, open.

Media keys are global (play/pause, next, volume...): they reach whatever player is
playing without focusing its window, and need no approval.
"""
from __future__ import annotations

import os
import re
import subprocess

from ..i18n import tr
from ..tools import control
from .base import Action, Param, Skill, Unusable

# what people call apps -> the app name windows report (control.app_name) / Start-menu name
APP_ALIASES = {
    "телеграм": "telegram", "телега": "telegram", "телеграмм": "telegram",
    "хром": "chrome", "гугл хром": "chrome", "браузер": "chrome", "prehliadač": "chrome",
    "спотифай": "spotify", "спотик": "spotify",
    "проводник": "file explorer", "explorer": "file explorer", "prieskumník": "file explorer",
    "терминал": "terminal", "консоль": "terminal",
    "дискорд": "discord", "стим": "steam", "блокнот": "notepad", "poznámkový blok": "notepad",
    "калькулятор": "calculator", "kalkulačka": "calculator", "ворд": "word", "эксель": "excel",
    "вс код": "vs code", "vscode": "vs code", "visual studio code": "vs code", "код": "vs code",
}
PROTECTED = {"file explorer", "explorer"}  # killing it takes the taskbar down with it
MEDIA = {"play_pause": "media_play_pause", "next": "media_next", "previous": "media_prev", "mute": "volume_mute",
         "volume_up": "volume_up", "volume_down": "volume_down"}

_P = r"(?:,?\s*(?:пожалуйста|please|prosím|плиз|pls))?[.!?]*"
QUICK_MEDIA = [
    (re.compile(rf"^(?:пауза|поставь на паузу|останови музыку|продолжи(?: музыку)?|pause|resume|play|pauza|"
                rf"pozastav|pokračuj){_P}$", re.I), "play_pause", 1),
    (re.compile(rf"^(?:следующ\w*(?: трек| песн\w*)?|дальше|next(?: track| song)?|skip|ďalš\w*(?: skladb\w*)?)"
                rf"{_P}$", re.I), "next", 1),
    (re.compile(rf"^(?:предыдущ\w*(?: трек| песн\w*)?|назад|previous(?: track| song)?|predchádzajúc\w*)"
                rf"{_P}$", re.I), "previous", 1),
    (re.compile(rf"^(?:(?:сделай |чуть )?громче|louder|volume up|turn it up|hlasnejšie){_P}$", re.I), "volume_up", 5),
    (re.compile(rf"^(?:(?:сделай |чуть )?тише|quieter|volume down|turn it down|tichšie){_P}$", re.I), "volume_down", 5),
    (re.compile(rf"^(?:без звука|выключи звук|включи звук|mute|unmute|stlm(?:iť)?(?: zvuk)?){_P}$", re.I), "mute", 1),
]
QUICK_APP = [
    (re.compile(rf"^(?:принудительно (?:закрой|заверши)|убей|заверши(?: процесс)?|force[- ]?(?:close|quit)|kill|"
                rf"násilne zatvor|ukonči)\s+(.+?){_P}$", re.I), "close_app", True),
    (re.compile(rf"^(?:закрой|close|quit|zatvor)\s+(.+?){_P}$", re.I), "close_app", False),
    (re.compile(rf"^(?:скрой|сверни|спрячь|hide|minimi[sz]e|skry|minimalizuj)\s+(.+?){_P}$", re.I), "hide_app", False),
    (re.compile(rf"^(?:покажи|разверни|верни|show|restore|unhide|zobraz|obnov)\s+(.+?){_P}$", re.I), "show_app", False),
    (re.compile(rf"^(?:открой|запусти|включи|open|launch|start|run|otvor|spusti)\s+(.+?){_P}$", re.I), "launch_app", False),
]


def canonical(name: str) -> str:
    n = " ".join(name.lower().replace("ё", "е").split()).strip(" .!?\"'«»")
    for prefix in ("приложение ", "программу ", "окно ", "app ", "the ", "aplikáciu "):
        if n.startswith(prefix):
            n = n[len(prefix):]
    return APP_ALIASES.get(n, n)


def app_windows(app, name: str) -> list[tuple[int, str]]:
    """(hwnd, title) of every window (open or minimized) of the app called `name` - by the
    program's name ("calculator") or the window title in any language ("Калькулятор")."""
    wants = {w for w in (canonical(name), " ".join(name.lower().split()).strip(" .!?\"'«»")) if w}
    found = [(w.hwnd, w.title) for w in app.scene.windows] + control.minimized_windows(os.getpid())
    exact = [(h, t) for h, t in found if control.app_name(h).lower() in wants]
    if exact:
        return exact
    return [(h, t) for h, t in found
            if any(w in control.app_name(h).lower() or w in t.lower() for w in wants)]


class SystemSkill(Skill):
    name = "system"
    KINDS = ("media", "close_app", "hide_app", "show_app")

    def actions(self, settings):
        app = Param("STRING", "app name as people say it: Telegram, Chrome, Spotify...")
        return [
            Action("media", "play / pause, next, previous track, volume, mute - for ANY player, no window needed",
                   {"media": Param("STRING", "which key", tuple(MEDIA)),
                    "times": Param("INTEGER", "volume steps (1 step = 2%), default 5")},
                   ('"pause the music" -> {"kind":"media","media":"play_pause"}',)),
            Action("close_app", "close ALL windows of an app by name; force = true kills it (unsaved work is lost)",
                   {"app": app, "force": Param("BOOLEAN", "kill the process (only if asked to force it)")},
                   ('"close telegram" -> {"kind":"close_app","app":"Telegram"}',)),
            Action("hide_app", "minimize all windows of an app by name", {"app": app}),
            Action("show_app", "bring back / show an app's windows by name (not for starting it: use launch_app)",
                   {"app": app}),
        ]

    # ------------------------------------------------------------------ quick
    def quick(self, text, app):
        for rx, key, times in QUICK_MEDIA:
            if rx.match(text):
                return [{"kind": "media", "media": key, "times": times}]
        for rx, kind, force in QUICK_APP:
            m = rx.match(text)
            if not m:
                continue
            name = m.group(1)
            if kind == "launch_app":  # only when it really is an app we can find (else let the AI think)
                if app_windows(app, name) or app.start_apps.find(canonical(name)):
                    return [{"kind": "launch_app", "app": canonical(name)}]
                return None
            if app_windows(app, name):
                step = {"kind": kind, "app": name}
                if kind == "close_app":
                    step["force"] = force
                return [step]
            return None
        return None

    # ---------------------------------------------------------------- resolve
    def resolve(self, runner, step, targets):
        kind, s = step["kind"], runner.app.roster.settings
        if kind == "media":
            key = str(step.get("media", ""))
            if key not in MEDIA:
                raise Unusable(f"media must be one of {', '.join(MEDIA)}")
            times = step.get("times")
            times = min(max(int(times), 1), 25) if isinstance(times, (int, float)) else (5 if "volume" in key else 1)
            return {"kind": kind, "skill": self.name, "key": MEDIA[key], "times": times, "trusted": True,
                    "what": tr("sk.media." + key)}
        name = str(step.get("app") or step.get("text") or "").strip()
        if not name:
            raise Unusable("missing app name")
        wins = app_windows(runner.app, name)
        if not wins:
            raise Unusable(f"no open window of {name!r}" + (" (to start it use launch_app)" if kind == "show_app" else ""))
        shown = control.app_name(wins[0][0]) or name
        if kind == "close_app" and step.get("force"):
            if canonical(name) in PROTECTED or canonical(shown) in PROTECTED:
                raise Unusable("File Explorer can't be force-closed (the taskbar would go with it): use close_app")
            return {"kind": kind, "skill": self.name, "wins": wins, "force": True, "trusted": False,
                    "what": tr("sk.kill", app=shown)}
        key = {"close_app": "sk.close", "hide_app": "sk.hide", "show_app": "sk.show"}[kind]
        return {"kind": kind, "skill": self.name, "wins": wins, "force": False, "trusted": s.trust_windows,
                "what": tr(key, app=shown, n=len(wins))}

    # -------------------------------------------------------------------- run
    def run(self, runner, s, spec):
        from ..tools.tasks import TaskFailed
        kind = spec["kind"]
        if kind == "media":
            s.jump()
            for _ in range(spec["times"]):
                control.press_combo([spec["key"]])
                yield ("wait", 0.05)
            return
        hwnds = [h for h, _t in spec["wins"] if control.exists(h)]
        if kind == "hide_app":
            for h in hwnds:
                control.minimize_window(h)
            return
        if kind == "show_app":
            s.wave(1.0)
            for h in hwnds:
                control.restore_window(h)
                yield ("wait", 0.15)
            return
        s.jump()  # close_app: the stomp
        yield ("wait", 0.3)
        if spec["force"]:
            pids = {control.real_pid(h) for h in hwnds} - {os.getpid(), 0}  # never the shared Store-app host
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                               creationflags=0x08000000)
        else:
            for h in hwnds:
                control.close_window(h)
        yield ("wait", 1.5)
        left = [h for h in hwnds if control.exists(h)]
        if left:
            raise TaskFailed(tr("sk.still_open", n=len(left)))
