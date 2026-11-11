"""Explorer: find a file or folder by name and show it; open known folders.

Search order:
1. the Windows Search index (instant, covers your user folders);
2. if that finds nothing good, a quick walk of your folders and the roots of your
   drives (a few levels deep, skipping Windows / Program Files / AppData /
   node_modules / .git...), with a time limit.
Found items are SHOWN (Explorer opens with the item selected). A file is only
opened itself when asked, and never if it's a program or script.
"""
from __future__ import annotations

import ctypes
import os
import re
import string
import subprocess
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from urllib.parse import unquote

from ..i18n import tr
from .base import Action, Param, Skill, Unusable, background

SKIP_DIRS = {"windows", "program files", "program files (x86)", "programdata", "$recycle.bin", "appdata",
             "system volume information", "node_modules", ".git", ".venv", "venv", "__pycache__", "site-packages",
             "$windows.~ws", "$winreagent", "recovery", "perflogs", "msocache", ".cache", ".gradle", ".m2"}
RUNNABLE = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".msi", ".scr", ".com", ".lnk",
            ".reg", ".hta", ".cpl", ".jar", ".py", ".pyw"}
SCAN_SECONDS = 3.5
CREATE_NO_WINDOW = 0x08000000

KNOWN = {  # name people use -> KNOWNFOLDERID
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}", "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}", "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "home": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
}
KNOWN_ALIASES = {
    "загрузки": "downloads", "закачки": "downloads", "download": "downloads", "stiahnuté": "downloads",
    "документы": "documents", "мои документы": "documents", "dokumenty": "documents",
    "рабочий стол": "desktop", "plocha": "desktop", "изображения": "pictures", "картинки": "pictures",
    "фото": "pictures", "obrázky": "pictures", "музыка": "music", "hudba": "music", "видео": "videos",
    "videá": "videos", "домашняя папка": "home", "домашнюю папку": "home", "моя папка": "home",
}
_P = r"(?:,?\s*(?:пожалуйста|please|prosím))?[.!?]*"
QUICK_OPEN_KNOWN = re.compile(rf"^(?:открой|покажи|open|show|otvor)\s+(?:папку\s+|folder\s+|the\s+|priečinok\s+)?(.+?){_P}$",
                              re.I)
# only unambiguous forms ("найди котиков" could be a web search: that's for the AI to decide)
QUICK_FIND = [
    re.compile(rf"^(?:найди|отыщи|покажи|find|locate|nájdi)\s+(?:мне\s+)?(файл|папку|file|folder|súbor|priečinok)\s+"
               rf"(.+?){_P}$", re.I),
    re.compile(rf"^(?:где|покажи где|where is|where's|where are|kde je|kde sú)\s+"
               rf"(?:(?:у меня|у тебя)\s+)?(?:(?:лежит|лежат|находится|находятся|хранится|сохранён\w*)\s+)?"
               rf"(?:(?:мой|моя|моё|мои|my|the)\s+)?"
               rf"(файл|папка|file|folder|súbor|priečinok)?\s*(.+?){_P}$", re.I),
]


def known_folder(name: str) -> str | None:
    n = re.sub(r"^(?:my|мои|мою|мой|моя|папку|папка|the|moje|môj)\s+", "", name.lower().strip())
    key = KNOWN_ALIASES.get(n, n)
    guid = KNOWN.get(key)
    if guid is None:
        return None
    ptr = ctypes.c_wchar_p()
    g = uuid.UUID(guid)
    try:
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(_GUID.from_uuid(g)), 0, None,
                                                       ctypes.byref(ptr)) == 0:
            path = ptr.value
            ctypes.windll.ole32.CoTaskMemFree(ptr)
            return path
    except OSError:
        pass
    return str(Path.home() / key.title()) if key != "home" else str(Path.home())


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_uuid(cls, u: uuid.UUID) -> "_GUID":
        b = u.bytes_le
        return cls(int.from_bytes(b[0:4], "little"), int.from_bytes(b[4:6], "little"),
                   int.from_bytes(b[6:8], "little"), (ctypes.c_ubyte * 8)(*b[8:16]))


# ---------------------------------------------------------------- searching
def _index(query: str, kind: str, limit: int = 30) -> list[str]:
    import comtypes
    import comtypes.client
    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    words = [w.replace("'", "''") for w in query.split() if w]
    where = " AND ".join(f"System.FileName LIKE '%{w}%'" for w in words) or "1=0"
    if kind == "folder":
        where += " AND System.ItemType = 'Directory'"
    elif kind == "file":
        where += " AND System.ItemType <> 'Directory'"
    conn = comtypes.client.CreateObject("ADODB.Connection", dynamic=True)
    conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
    rs = conn.Execute(f"SELECT TOP {limit} System.ItemUrl FROM SYSTEMINDEX WHERE {where} "
                      "ORDER BY System.DateModified DESC")
    rs = rs[0] if isinstance(rs, tuple) else rs
    out = []
    while not rs.EOF:
        url = str(rs.Fields.Item(0).Value or "")
        if url.lower().startswith("file:"):
            out.append(os.path.normpath(unquote(url[5:])))
        rs.MoveNext()
    return out


def _drives() -> list[str]:
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [f"{c}:\\" for i, c in enumerate(string.ascii_uppercase)
            if mask >> i & 1 and ctypes.windll.kernel32.GetDriveTypeW(f"{c}:\\") == 3]  # fixed disks


def _scan(query: str, kind: str, limit: int = 30) -> list[str]:
    """Breadth-first walk with a time budget."""
    words = [w.lower() for w in query.split() if w]
    roots = [(p, 5) for p in (known_folder(k) for k in ("desktop", "documents", "downloads", "pictures", "music",
                                                         "videos", "home")) if p]
    roots += [(d, 4) for d in _drives()]
    seen, out, queue = set(), [], list(roots)
    deadline = time.monotonic() + SCAN_SECONDS
    while queue and time.monotonic() < deadline and len(out) < limit:
        folder, depth = queue.pop(0)
        key = os.path.normcase(folder)
        if key in seen:
            continue
        seen.add(key)
        try:
            with os.scandir(folder) as it:
                for e in it:
                    name = e.name.lower()
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if all(w in name for w in words) and (kind == "any" or (kind == "folder") == is_dir):
                        out.append(e.path)
                    if is_dir and depth > 0 and name not in SKIP_DIRS and not name.startswith("."):
                        queue.append((e.path, depth - 1))
        except OSError:
            continue
    return out


def _rank(paths: list[str], query: str) -> list[str]:
    q = query.lower().strip()

    def score(p: str):
        name = os.path.basename(p).lower()
        stem = os.path.splitext(name)[0]
        return (0 if q in (name, stem) else 1 if name.startswith(q) else 2, p.count(os.sep), len(p))
    uniq = list(dict.fromkeys(os.path.normpath(p) for p in paths if os.path.exists(p)))
    return sorted(uniq, key=score)


def find(query: str, kind: str = "any") -> list[str]:
    try:
        hits = _index(query, kind)
    except Exception:  # noqa: BLE001 - index off / unavailable: walk instead
        hits = []
    ranked = _rank(hits, query)
    exact = [p for p in ranked if os.path.splitext(os.path.basename(p).lower())[0] == query.lower()
             or os.path.basename(p).lower() == query.lower()]
    if not exact:  # the index only covers some places (often not other drives): walk too
        ranked = _rank(hits + _scan(query, kind), query)
    return ranked[:10]


def reveal(path: str) -> None:
    """Explorer with the item selected (a folder: opened)."""
    if os.path.isdir(path):
        subprocess.Popen(["explorer", os.path.normpath(path)])
    else:
        subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')


class ExplorerSkill(Skill):
    name = "explorer"
    KINDS = ("find_path", "open_folder")

    def actions(self, settings):
        return [
            Action("find_path", "find a file or folder on this PC by (part of) its name and show it in Explorer; "
                                "open = true also opens a found document / picture (never programs)",
                   {"query": Param("STRING", "the name or words in the name, e.g. 'StickAI' or 'отчёт март'"),
                    "type": Param("STRING", "what to look for", ("any", "file", "folder")),
                    "open": Param("BOOLEAN", "open the found file itself (not just show where it is)")},
                   ('"where is my StickAI folder" -> {"kind":"find_path","query":"StickAI","type":"folder"}',)),
            Action("open_folder", "open a folder: a full path, or downloads / documents / desktop / pictures / "
                                  "music / videos / home",
                   {"path": Param("STRING", "full path or one of the known names")}),
        ]

    def quick(self, text, app):
        m = QUICK_OPEN_KNOWN.match(text)
        if m and known_folder(m.group(1)):
            return [{"kind": "open_folder", "path": m.group(1)}]
        for rx in QUICK_FIND:
            m = rx.match(text)
            if m and m.group(2).strip():
                what = (m.group(1) or "").lower()
                q, _, tail = m.group(2).strip().rpartition(" ")
                if q and tail.lower() in ("folder", "file", "папка", "файл", "priečinok", "súbor"):  # "my X folder"
                    return [{"kind": "find_path", "query": q.strip(" \"'«»"),
                             "type": "folder" if tail.lower() in ("folder", "папка", "priečinok") else "file"}]
                kind = "folder" if what in ("папку", "папка", "folder", "priečinok") else "file" if what else "any"
                return [{"kind": "find_path", "query": m.group(2).strip(" \"'«»"), "type": kind}]
        return None

    def resolve(self, runner, step, targets):
        if step["kind"] == "open_folder":
            raw = str(step.get("path") or step.get("text") or "").strip().strip('"')
            path = known_folder(raw) or (raw if os.path.isabs(raw) and os.path.isdir(raw) else None)
            if path is None:
                raise Unusable(f"no folder {raw!r}: give a full path, a known name, or use find_path")
            return {"kind": "open_folder", "skill": self.name, "path": path, "trusted": True,
                    "what": tr("sk.open_folder", path=path)}
        query = " ".join(str(step.get("query") or step.get("text") or "").split()).strip(" \"'«»")
        if not query:
            raise Unusable("missing query (the name to look for)")
        kind = str(step.get("type", "any"))
        return {"kind": "find_path", "skill": self.name, "query": query,
                "type": kind if kind in ("any", "file", "folder") else "any",
                "open": bool(step.get("open")), "trusted": True, "what": tr("sk.find", query=query)}

    def run(self, runner, s, spec):
        from ..tools.tasks import TaskFailed
        if spec["kind"] == "open_folder":
            s.jump()
            subprocess.Popen(["explorer", spec["path"]])
            return
        s.gesture, s.gesture_until = "type", s.time + 3.0  # rummaging around
        box = background(lambda: find(spec["query"], spec["type"]))
        yield ("until", lambda: box.get("done", False), SCAN_SECONDS + 4)
        s.gesture = ""
        hits = box.get("value") or []
        if not hits:
            raise TaskFailed(tr("sk.not_found", query=spec["query"]))
        best = hits[0]
        if spec["open"] and os.path.isfile(best) and os.path.splitext(best)[1].lower() not in RUNNABLE:
            os.startfile(best)
        else:
            reveal(best)
        s.jump()
        spec["done"] = tr("sk.found", query=spec["query"], path=best)
        others = hits[1:5]
        spec["report"] = f"showed {best}" + (f"; other matches: {'; '.join(others)}" if others else "")
