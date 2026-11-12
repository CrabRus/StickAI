"""VS Code: open a folder (a project) or a single file in VS Code.

"открой StickAI в VS Code", "open filemanager.py in vscode", "открой загрузки в коде".
The path is a full path, a known folder, or found by name (Windows Search index, then a
quick walk, like the Explorer skill). Opening is harmless (nothing runs), so it doesn't ask.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from ..i18n import tr
from .base import Action, Param, Skill, Unusable, background
from .explorer import SCAN_SECONDS, find, known_folder

CREATE_NO_WINDOW = 0x08000000
# "открой X в VS Code" / "open X in vscode" / "otvor X vo VS Code"
_EDITOR = r"(?:vs ?code|visual studio code|вс ?код\w*|вскод\w*|вижуал студио код\w*|код[еу]|code)"
QUICK_OPEN = re.compile(
    rf"^(?:открой|открыть|запусти|open|otvor)\s+(?:(папку|проект|файл|folder|project|file|priečinok|projekt|súbor)\s+)?"
    rf"(.+?)\s+(?:в|во|in|with|vo|v)\s+{_EDITOR}(?:,?\s*(?:пожалуйста|please|prosím))?[.!?]*$", re.I)
FOLDER_WORDS = ("папку", "проект", "folder", "project", "priečinok", "projekt")
FILE_WORDS = ("файл", "file", "súbor")


def code_exe() -> list[str] | None:
    """How to start VS Code: Code.exe itself (no console flash), else the `code` command."""
    candidates = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Microsoft VS Code", "Code.exe"),
                  os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Microsoft VS Code", "Code.exe")]
    cli = shutil.which("code") or shutil.which("code.cmd")
    if cli:  # ...\Microsoft VS Code\bin\code.cmd -> ...\Microsoft VS Code\Code.exe
        candidates.insert(0, os.path.join(os.path.dirname(os.path.dirname(cli)), "Code.exe"))
    for c in candidates:
        if c and os.path.isfile(c):
            return [c]
    return [cli] if cli else None


def open_in_code(path: str) -> None:
    exe = code_exe()
    if exe is None:
        raise OSError("VS Code not found")
    subprocess.Popen(exe + [os.path.normpath(path)], creationflags=CREATE_NO_WINDOW, close_fds=True)


class VSCodeSkill(Skill):
    name = "vscode"
    KINDS = ("open_in_vscode",)

    def __init__(self):
        self._has_code: bool | None = None

    def installed(self) -> bool:
        if self._has_code is None:
            self._has_code = code_exe() is not None
        return self._has_code

    def actions(self, settings):
        if not self.installed():
            return []
        return [Action(
            "open_in_vscode", "open a folder (project) or a single file in VS Code, by full path or by (part of) "
                              "its name - it is found automatically, no find_path needed",
            {"query": Param("STRING", "full path, a known folder (downloads, documents...) or the name, "
                                      "e.g. 'StickAI' or 'filemanager.py'"),
             "type": Param("STRING", "folder = a project folder, file = one file", ("any", "file", "folder"))},
            ('"open the StickAI project in VS Code" -> {"kind":"open_in_vscode","query":"StickAI","type":"folder"}',))]

    def quick(self, text, app):
        if not self.installed():
            return None
        m = QUICK_OPEN.match(text)
        if not m:
            return None
        what = (m.group(1) or "").lower()
        query = m.group(2).strip(" \"'«»")
        if not query:
            return None
        kind = "folder" if what in FOLDER_WORDS else "file" if what in FILE_WORDS else "any"
        return [{"kind": "open_in_vscode", "query": query, "type": kind}]

    def resolve(self, runner, step, targets):
        query = " ".join(str(step.get("query") or step.get("path") or step.get("text") or "").split()).strip(" \"'«»")
        if not query:
            raise Unusable("missing query (the folder / file name or path)")
        if not self.installed():
            raise Unusable("VS Code is not installed")
        kind = str(step.get("type", "any"))
        return {"kind": "open_in_vscode", "skill": self.name, "query": query,
                "type": kind if kind in ("any", "file", "folder") else "any", "trusted": True,
                "what": tr("sk.vscode_open", query=query)}

    def run(self, runner, s, spec):
        from ..tools.tasks import TaskFailed
        query = spec["query"]
        path = known_folder(query) or (os.path.normpath(query) if os.path.isabs(query) and os.path.exists(query)
                                       else None)
        if path is None:
            box = background(lambda: find(query, spec["type"]))
            yield ("until", lambda: box.get("done", False), SCAN_SECONDS + 4)
            hits = box.get("value") or []
            if spec["type"] == "any":  # "open StickAI": a project folder beats a file of the same name
                stem = query.lower()
                folders = [h for h in hits if os.path.isdir(h) and os.path.basename(h).lower() == stem]
                hits = folders + [h for h in hits if h not in folders]
            if not hits:
                raise TaskFailed(tr("sk.not_found", query=query))
            path = hits[0]
            others = hits[1:4]
        else:
            others = []
        try:
            open_in_code(path)
        except OSError as e:
            raise TaskFailed(tr("sk.vscode_failed", error=str(e)))
        s.jump()
        spec["done"] = tr("sk.vscode_opened", path=path)
        spec["report"] = f"opened {path} in VS Code" + (f"; other matches: {'; '.join(others)}" if others else "")
