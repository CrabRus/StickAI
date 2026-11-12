"""Files: move / copy / delete / list YOUR files by type, name and date.

"перенеси все pdf за сегодня из загрузок в документы", "copy today's photos from Downloads to Pictures\\Trip",
"delete the .tmp files in Downloads", "какие архивы я скачал на этой неделе?", "отмени" (undo).
Creating: new (empty or with text) files / folders in any of your folders ("создай ещё 3 файла
like this one" after the folder was found / listed). Never over an existing name; always asks; undo
puts untouched new files in the Recycle Bin.
Renaming: one file / folder found by an approximate name ("переименуй задание CV6 в «ML задание 6»"),
or many at once with a pattern ("пронумеруй сегодняшние скриншоты: Проект 1, Проект 2...").

Safety:
* the Allow dialog shows how many files and which (list never asks);
* delete = to the Recycle Bin only (always asks);
* nothing is overwritten: a clash becomes "name (1).pdf";
* at most MAX_FILES per operation, hidden / system files are skipped;
* renames keep the file's extension, never replace an existing name, and always ask;
* the last move / copy / rename is journaled (memory/file_ops.json) so "undo" can put it back.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import time
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

from ..i18n import tr
from .base import Action, Param, Skill, Unusable, background
from .explorer import _index, _rank, known_folder

MAX_FILES = 500
MAX_CREATE = 50
MAX_CONTENT = 200_000
SCAN_LIMIT = 20000
CATEGORIES = {
    "images": "jpg jpeg png gif webp bmp heic heif tif tiff svg ico raw cr2 nef",
    "documents": "pdf doc docx xls xlsx ppt pptx txt rtf odt ods odp md csv epub",
    "pdf": "pdf",
    "video": "mp4 mkv avi mov webm wmv flv m4v",
    "audio": "mp3 wav flac m4a ogg aac wma opus",
    "archives": "zip rar 7z tar gz bz2 xz",
    "installers": "exe msi msix appx",
    "torrents": "torrent",
}
CATEGORY_WORDS = {
    "картинки": "images", "изображения": "images", "фото": "images", "фотографии": "images", "скриншоты": "images",
    "pictures": "images", "photos": "images", "screenshots": "images", "obrázky": "images", "fotky": "images",
    "документы": "documents", "docs": "documents", "dokumenty": "documents",
    "видео": "video", "videos": "video", "videá": "video", "музыка": "audio", "music": "audio", "hudba": "audio",
    "песни": "audio", "архивы": "archives", "archive": "archives", "archívy": "archives",
    "установщики": "installers", "инсталляторы": "installers",
}
WHEN = ("any", "today", "yesterday", "this_week", "last_7_days", "this_month", "last_30_days")
JUNK = {"desktop.ini", "thumbs.db"}
BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
RESERVED = re.compile(r"^(?:con|prn|aux|nul|com\d|lpt\d)(?:\..*)?$", re.I)
UNDO_QUICK = re.compile(r"^(?:отмени(?: (?:это|перенос|последнее|копирование))?|верни (?:файлы|всё|все)(?: обратно| назад)?|"
                        r"undo(?: that| it| the move)?|put (?:them|it) back|vráť(?: to)?(?: späť)?|zruš to)[.!]*$", re.I)


def _extensions(types: str) -> set[str]:
    out = set()
    for t in re.split(r"[,\s;/]+", types.lower()):
        t = t.strip().lstrip("*.")
        if not t:
            continue
        t = CATEGORY_WORDS.get(t, t)
        out |= set(CATEGORIES.get(t, t).split())
    return out


def _since(when: str) -> tuple[float, float]:
    now = datetime.now()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = {"today": day, "yesterday": day - timedelta(days=1), "this_week": day - timedelta(days=day.weekday()),
             "last_7_days": now - timedelta(days=7), "this_month": day.replace(day=1),
             "last_30_days": now - timedelta(days=30)}.get(when)
    end = day if when == "yesterday" else now + timedelta(days=1)
    return (start.timestamp() if start else 0.0), end.timestamp()


def resolve_folder(raw: str, create: bool = False) -> str:
    """'downloads', 'Документы\\PDF', 'D:\\Work', 'StickAI' -> a real folder path (Unusable if not)."""
    raw = raw.strip().strip('"').rstrip("\\/")
    if not raw:
        raise Unusable("missing folder")
    k = known_folder(raw)
    if k:
        return k
    if os.path.isabs(raw):
        if os.path.isdir(raw) or (create and os.path.isdir(os.path.dirname(raw))):
            return os.path.normpath(raw)
        raise Unusable(f"folder {raw!r} doesn't exist")
    parts = re.split(r"[\\/]", raw, maxsplit=1)
    head, rest = parts[0], (parts[1].strip("\\/") if len(parts) > 1 else "")
    base = known_folder(head)
    if base and rest:  # "documents/PDF": inside a known folder (created if it's the destination)
        path = os.path.join(base, rest)
        if os.path.isdir(path) or create:
            return os.path.normpath(path)
        raise Unusable(f"folder {raw!r} doesn't exist")
    try:  # a folder somewhere, by name (index only: instant)
        hits = [p for p in _index(raw, "folder", 10) if os.path.basename(p).lower() == raw.lower()]
    except Exception:  # noqa: BLE001
        hits = []
    if hits:
        return sorted(hits, key=len)[0]
    raise Unusable(f"no folder {raw!r} found: use a full path, a known folder (downloads, documents, desktop, "
                   f"pictures, music, videos) or find_path first")


def matching_files(folder: str, exts: set[str], name: str, when: str, sub: bool, newest: int) -> list[str]:
    lo, hi = _since(when)
    name = name.lower()
    out: list[tuple[float, str]] = []
    seen = 0
    walker = os.walk(folder) if sub else [(folder, [], [e.name for e in os.scandir(folder) if e.is_file()])]
    for root, dirs, files in walker:
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in ("node_modules", "$recycle.bin")]
        for f in files:
            seen += 1
            if seen > SCAN_LIMIT:
                break
            low = f.lower()
            if low in JUNK or low.startswith((".", "~$")):
                continue
            if exts and os.path.splitext(low)[1].lstrip(".") not in exts:
                continue
            if name and name not in low:
                continue
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            t = max(st.st_ctime, st.st_mtime)  # downloaded / created / changed: whichever is newest
            if lo <= t < hi:
                out.append((t, p))
    out.sort(reverse=True)
    if newest > 0:
        out = out[:newest]
    return [p for _t, p in out]


def unique_target(folder: str, filename: str) -> str:
    target = os.path.join(folder, filename)
    stem, ext = os.path.splitext(filename)
    i = 1
    while os.path.exists(target):
        target = os.path.join(folder, f"{stem} ({i}){ext}")
        i += 1
    return target


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def to_recycle_bin(paths: list[str]) -> bool:
    """Delete to the Recycle Bin (restorable), silently. True if it worked."""
    if not paths:
        return True
    op = _SHFILEOPSTRUCTW(None, 3, "\0".join(os.path.abspath(p) for p in paths) + "\0\0", None,
                          0x0040 | 0x0010 | 0x0004 | 0x0400)  # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not op.fAnyOperationsAborted


def clean_name(new: str, old_path: str) -> str:
    """A safe new file name: no path parts / forbidden characters; a file keeps its extension."""
    new = " ".join(str(new).split()).strip(" .\"'«»")
    if not new or BAD_NAME.search(new) or RESERVED.match(new):
        raise Unusable(f"bad new name {new!r}: no \\ / : * ? \" < > | and not empty")
    if len(new) > 150:
        raise Unusable("the new name is too long")
    if os.path.isdir(old_path):
        return new
    ext = os.path.splitext(old_path)[1]
    if ext and new.lower().endswith(ext.lower()):
        new = new[: -len(ext)].rstrip(" .")
        if not new:
            raise Unusable("the new name is empty")
    return new + ext


def find_item(query: str, kind: str, folder: str | None) -> list[str]:
    """Files / folders whose name contains all the words of query: in one folder (2 levels), else the index."""
    words = [w.lower() for w in re.split(r"[\s_\-.]+", query) if w]
    hits: list[str] = []
    if folder:
        for root, dirs, files in os.walk(folder):
            depth = root[len(folder):].count(os.sep)
            dirs[:] = [d for d in dirs if not d.startswith(".") and depth < 1]
            names = (files if kind != "folder" else []) + (dirs if kind != "file" else [])
            for n in names:
                low = n.lower()
                if all(w in low for w in words) and low not in JUNK:
                    hits.append(os.path.join(root, n))
    else:
        try:
            hits = _index(" ".join(words), kind, 20)
        except Exception:  # noqa: BLE001 - index unavailable
            hits = []
    return _rank(hits, query)


def apply_pattern(pattern: str, path: str, n: int, width: int) -> str:
    """'Trip {n}' -> 'Trip 01'; {name} = the old name, {date} = its date; the extension is kept."""
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        date = datetime.fromtimestamp(os.stat(path).st_mtime).strftime("%Y-%m-%d")
    except OSError:
        date = ""
    out = pattern.replace("{n}", str(n).zfill(width)).replace("{name}", stem).replace("{date}", date)
    if "{n}" not in pattern and "{name}" not in pattern:
        out += f" {str(n).zfill(width)}"  # several files can't all get the same name
    return out


def _short(paths: list[str], n: int = 8) -> str:
    names = [os.path.basename(p) for p in paths[:n]]
    return ", ".join(names) + (f" … (+{len(paths) - n})" if len(paths) > n else "")


class FileManagerSkill(Skill):
    name = "files"
    KINDS = ("manage_files", "rename_path", "create_files", "undo_files")

    def __init__(self):
        self.journal_path: Path | None = None

    def actions(self, settings):
        return [
            Action("create_files",
                   "create new files (empty, or with the given text) or folders in a folder of the user. If the "
                   "names depend on what is already there and you haven't seen the folder's files yet, list it "
                   "first (manage_files list + continue).",
                   {"in": Param("STRING", "the folder: a full path (best), downloads / documents / desktop..., "
                                          "'documents/Notes', or a folder name"),
                    "names": Param("STRING", "REQUIRED for create_files: the new names WITH extensions, separated "
                                             "by | e.g. 'task_2.c|task_3.c'"),
                    "content": Param("STRING", "create_files: optional text written into every new file"),
                    "folders": Param("BOOLEAN", "create_files: true = create folders instead of files")},
                   ('"in C:\\Work\\CV6 (it has lab_6_1.c) create 2 more like it" -> {"kind":"create_files",'
                    '"in":"C:\\Work\\CV6","names":"lab_6_2.c|lab_6_3.c"}',)),
            Action("rename_path",
                   "rename ONE file or folder. Find it by an approximate name (words from its name, in any order) "
                   "or a full path; it is searched automatically. The extension is kept.",
                   {"query": Param("STRING", "words from the current name, or the full path"),
                    "new_name": Param("STRING", "REQUIRED for rename_path: the new name the user wants "
                                                "(without the extension)"),
                    "in": Param("STRING", "optional: the folder it is in (downloads, documents, a path...)"),
                    "type": Param("STRING", "what it is", ("any", "file", "folder"))},
                   ('"rename the CV6 assignment in downloads to ML task 6" -> {"kind":"rename_path",'
                    '"query":"CV6","new_name":"ML task 6","in":"downloads"}',)),
            Action("manage_files",
                   "move / copy / delete (to the Recycle Bin) / list / rename MANY of the user's files in a folder, "
                   "filtered by type, name and date. Shows the user exactly which files before doing it.",
                   {"op": Param("STRING", "what to do", ("move", "copy", "delete", "list", "rename")),
                    "from": Param("STRING", "source folder: downloads, documents, desktop, pictures, music, videos, "
                                            "a full path, or a folder name"),
                    "to": Param("STRING", "destination for move / copy; may be a NEW subfolder like 'documents/PDF'"),
                    "types": Param("STRING", "extensions or categories, e.g. 'pdf' or 'jpg,png' or 'images' / "
                                             "'documents' / 'video' / 'audio' / 'archives'; empty = all"),
                    "name": Param("STRING", "only files whose name contains this"),
                    "when": Param("STRING", "by date (downloaded / created / changed)", WHEN),
                    "newest": Param("INTEGER", "only the N newest (e.g. 1 = the last downloaded file)"),
                    "subfolders": Param("BOOLEAN", "include subfolders"),
                    "pattern": Param("STRING", "rename: the new names; {n} = number (oldest first), {name} = the old "
                                               "name, {date} = its date, e.g. 'Trip {n}' or 'ML_{name}'")},
                   ('"move all today\'s pdfs from downloads to documents" -> {"kind":"manage_files","op":"move",'
                    '"from":"downloads","to":"documents","types":"pdf","when":"today"}',
                    '"what did I download this week?" -> {"kind":"manage_files","op":"list","from":"downloads",'
                    '"when":"this_week"}',
                    '"number today\'s screenshots on the desktop as Project 1, 2..." -> {"kind":"manage_files",'
                    '"op":"rename","from":"desktop","types":"png","when":"today","pattern":"Project {n}"}')),
            Action("undo_files", "undo the last move / copy / rename (puts the files back / removes the copies)", {}),
        ]

    def rules(self, settings):
        return ("For list, add \"continue\": true and tell the user what you found. Never delete unless the user "
                "clearly asked to delete.")

    def quick(self, text, app):
        j = self._journal(app)
        if UNDO_QUICK.match(text) and (j.get("pairs") or j.get("created")):
            return [{"kind": "undo_files"}]
        return None

    # ---------------------------------------------------------------- journal
    def _jpath(self, app) -> Path:
        return app.characters_path.parent / "memory" / "file_ops.json"

    def _journal(self, app) -> dict:
        try:
            return json.loads(self._jpath(app).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_journal(self, app, data: dict) -> None:
        p = self._jpath(app)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ---------------------------------------------------------------- resolve
    def resolve(self, runner, step, targets):
        app = runner.app
        if step["kind"] == "undo_files":
            j = self._journal(app)
            if not j.get("pairs") and not j.get("created"):
                raise Unusable("nothing to undo")
            n = len(j.get("pairs") or j.get("created") or [])
            return {"kind": "undo_files", "skill": self.name, "journal": j, "trusted": True,
                    "what": tr("fm.undo", n=n, op=tr("fm.op_" + j.get("op", "move")))}
        if step["kind"] == "rename_path":
            return self._resolve_rename(step)
        if step["kind"] == "create_files":
            return self._resolve_create(step)
        op = str(step.get("op", "list")).lower()
        if op not in ("move", "copy", "delete", "list", "rename"):
            raise Unusable("op must be move, copy, delete, list or rename")
        pattern = " ".join(str(step.get("pattern") or "").split())
        if op == "rename" and not pattern:
            raise Unusable("rename needs 'pattern' (e.g. 'Trip {n}' or 'ML_{name}')")
        src = resolve_folder(str(step.get("from") or "downloads"))
        dst = None
        if op in ("move", "copy"):
            if not step.get("to"):
                raise Unusable(f"{op} needs 'to' (the destination folder)")
            dst = resolve_folder(str(step["to"]), create=True)
            if os.path.normcase(dst) == os.path.normcase(src):
                raise Unusable("source and destination are the same folder")
        when = str(step.get("when") or "any")
        when = when if when in WHEN else "any"
        newest = step.get("newest")
        newest = int(newest) if isinstance(newest, (int, float)) and newest > 0 else 0
        files = matching_files(src, _extensions(str(step.get("types") or "")), str(step.get("name") or "").strip(),
                               when, bool(step.get("subfolders")), newest)
        if op != "list" and len(files) > MAX_FILES:
            raise Unusable(f"{len(files)} files match: too many at once (max {MAX_FILES}); narrow it down")
        where = {"src": os.path.basename(src) or src, "dst": os.path.basename(dst or "") or (dst or "")}
        if op == "list":
            what = tr("fm.list", n=len(files), src=where["src"])
        elif op == "rename":
            renames = self._plan_renames(files, pattern)
            return {"kind": "manage_files", "skill": self.name, "op": op, "src": src, "dst": None, "files": files,
                    "renames": renames, "trusted": not files,
                    "what": tr("fm.rename", n=len(files), src=where["src"]) + (":\n" + "\n".join(
                        f"{os.path.basename(a)} → {os.path.basename(b)}" for a, b in renames[:8]) +
                        (f"\n… (+{len(renames) - 8})" if len(renames) > 8 else "") if files else "")}
        else:
            what = tr("fm." + op, n=len(files), **where) + (f":\n{_short(files)}" if files else "")
        return {"kind": "manage_files", "skill": self.name, "op": op, "src": src, "dst": dst, "files": files,
                "trusted": op == "list" or not files, "what": what}

    # -------------------------------------------------------------------- run
    def run(self, runner, s, spec):
        from ..tools.tasks import TaskFailed
        app = runner.app
        if spec["kind"] == "undo_files":
            yield from self._undo(app, s, spec)
            return
        op, files = spec["op"], spec["files"]
        if op == "create":
            yield from self._create(app, s, spec)
            return
        if op == "list":
            lines = []
            for p in files[:60]:
                try:
                    st = os.stat(p)
                    lines.append(f"{os.path.basename(p)} ({st.st_size // 1024} KB, "
                                 f"{datetime.fromtimestamp(max(st.st_ctime, st.st_mtime)):%d.%m %H:%M})")
                except OSError:
                    continue
            spec["report"] = (f"{len(files)} file(s) in {spec['src']}:\n" + "\n".join(lines)) if files else \
                f"no matching files in {spec['src']}"
            spec["followup"] = True
            spec["done"] = tr("fm.listed", n=len(files), src=os.path.basename(spec["src"]))
            return
        if not files:
            raise TaskFailed(tr("fm.none"))

        def work():
            pairs, failed = [], []
            if op == "rename":
                for a, b in spec["renames"]:
                    try:
                        if os.path.exists(b):
                            raise OSError("taken")
                        os.rename(a, b)
                        pairs.append((a, b))
                    except OSError:
                        failed.append(a)
                return pairs, failed
            if op == "delete":
                return ([], []) if to_recycle_bin(files) else ([], files)
            os.makedirs(spec["dst"], exist_ok=True)
            for p in files:
                try:
                    target = unique_target(spec["dst"], os.path.basename(p))
                    (shutil.move if op == "move" else shutil.copy2)(p, target)
                    pairs.append((p, target))
                except OSError:
                    failed.append(p)
            return pairs, failed
        box = background(work)
        yield ("until", lambda: box.get("done", False), 600.0)
        pairs, failed = box.get("value") or ([], files)
        if op in ("move", "copy", "rename") and pairs:
            self._save_journal(app, {"op": op, "pairs": pairs, "time": time.time()})
        done = len(files) - len(failed)
        if done == 0:
            raise TaskFailed(tr("fm.failed_all"))
        s.jump()
        spec["done"] = tr("fm.done_" + op, n=done, dst=spec["dst"] or "", src=os.path.basename(spec["src"])) + \
            (tr("fm.some_failed", n=len(failed)) if failed else "")
        spec["report"] = spec["done"] + (f"; not done (in use?): {_short(failed)}" if failed else "")

    # ---------------------------------------------------------------- renaming
    def _resolve_rename(self, step) -> dict:
        query = " ".join(str(step.get("query") or step.get("path") or "").split()).strip(" \"'«»")
        if not query:
            raise Unusable("missing query (the current name or path)")
        if not str(step.get("new_name") or "").strip():
            raise Unusable("missing new_name")
        kind = str(step.get("type") or "any")
        kind = kind if kind in ("any", "file", "folder") else "any"
        if os.path.isabs(query) and os.path.exists(query):
            hits = [os.path.normpath(query)]
        else:
            folder = resolve_folder(str(step["in"])) if step.get("in") else None
            hits = find_item(query, kind, folder)
            if not hits and folder is None:  # not in the index: the usual folders, two levels deep
                for k in ("downloads", "desktop", "documents", "pictures"):
                    hits = find_item(query, kind, known_folder(k))
                    if hits:
                        break
        if not hits:
            raise Unusable(f"nothing named like {query!r} found: give 'in' (the folder), the full path, "
                           "or use find_path first")
        q = query.lower()
        exact = [h for h in hits if os.path.splitext(os.path.basename(h))[0].lower() == q
                 or os.path.basename(h).lower() == q]
        if len(exact) == 1:
            old = exact[0]
        elif len(hits) == 1:
            old = hits[0]
        else:  # never guess which file to rename
            raise Unusable("several match, put the full path of the right one in query: " + "; ".join(hits[:6]))
        new_name = clean_name(str(step["new_name"]), old)
        new = os.path.join(os.path.dirname(old), new_name)
        if os.path.normcase(new) == os.path.normcase(old):
            if new == old:
                raise Unusable("it already has that name")
        elif os.path.exists(new):
            raise Unusable(f"{new_name!r} already exists in that folder: choose another name")
        return {"kind": "manage_files", "skill": self.name, "op": "rename", "src": os.path.dirname(old), "dst": None,
                "files": [old], "renames": [(old, new)], "trusted": False,
                "what": tr("fm.rename_one", old=os.path.basename(old), new=new_name,
                           folder=os.path.dirname(old))}

    # ---------------------------------------------------------------- creating
    def _resolve_create(self, step) -> dict:
        raw = str(step.get("names") or step.get("name") or "")
        names = [" ".join(n.split()).strip(" \"'«»") for n in re.split(r"[|\n;]+|,(?=\s*\S+\.\w{1,8}\s*(?:,|$))", raw)]
        names = [n for n in names if n]
        if not names:
            raise Unusable("missing names (the new file names with extensions, separated by |)")
        if len(names) > MAX_CREATE:
            raise Unusable(f"too many at once (max {MAX_CREATE})")
        folder = resolve_folder(str(step.get("in") or step.get("to") or step.get("from") or ""), create=True)
        folders = bool(step.get("folders"))
        content = str(step.get("content") or "")
        if folders and content:
            content = ""
        if len(content) > MAX_CONTENT:
            raise Unusable("the text is too long")
        paths, seen = [], set()
        for n in names:
            if BAD_NAME.search(n) or RESERVED.match(n) or n in (".", ".."):
                raise Unusable(f"bad name {n!r}: no \\ / : * ? \" < > |")
            p = os.path.join(folder, n)
            if os.path.exists(p):
                raise Unusable(f"{n!r} already exists in {folder}: choose other names")
            if os.path.normcase(p) in seen:
                raise Unusable(f"{n!r} is listed twice")
            seen.add(os.path.normcase(p))
            paths.append(p)
        what = tr("fm.create_folders" if folders else "fm.create", n=len(paths), dst=folder) + ":\n" + \
            "\n".join(os.path.basename(p) for p in paths[:12]) + (f"\n… (+{len(paths) - 12})" if len(paths) > 12 else "")
        if content:
            preview = content if len(content) <= 160 else content[:160] + "…"
            what += "\n" + tr("fm.with_text", text=preview)
        return {"kind": "manage_files", "skill": self.name, "op": "create", "src": folder, "dst": folder,
                "files": paths, "folders": folders, "content": content, "trusted": False, "what": what}

    def _create(self, app, s, spec):
        from ..tools.tasks import TaskFailed

        def work():
            made, failed = [], []
            os.makedirs(spec["dst"], exist_ok=True)
            for p in spec["files"]:
                try:
                    if spec["folders"]:
                        os.mkdir(p)
                    else:
                        with open(p, "x", encoding="utf-8", newline="") as f:  # "x": never over an existing file
                            f.write(spec["content"])
                    made.append(p)
                except OSError:
                    failed.append(p)
            return made, failed
        box = background(work)
        yield ("until", lambda: box.get("done", False), 60.0)
        made, failed = box.get("value") or ([], spec["files"])
        if not made:
            raise TaskFailed(tr("fm.failed_all"))
        stamps = []
        for p in made:
            try:
                st = os.stat(p)
                stamps.append([p, st.st_size, st.st_mtime])
            except OSError:
                pass
        self._save_journal(app, {"op": "create", "created": stamps, "time": time.time()})
        s.jump()
        spec["done"] = tr("fm.done_create", n=len(made), dst=spec["dst"]) + \
            (tr("fm.some_failed", n=len(failed)) if failed else "")
        spec["report"] = f"created in {spec['dst']}: " + ", ".join(os.path.basename(p) for p in made) + \
            (f"; failed: {_short(failed)}" if failed else "")

    @staticmethod
    def _plan_renames(files: list[str], pattern: str) -> list[tuple[str, str]]:
        def mtime(p):
            try:
                return os.stat(p).st_mtime
            except OSError:
                return 0.0
        ordered = sorted(files, key=mtime)  # numbered oldest first
        width = len(str(len(ordered))) if len(ordered) >= 10 else 1
        out, used = [], set()
        for i, p in enumerate(ordered, 1):
            new_name = clean_name(apply_pattern(pattern, p, i, width), p)
            folder = os.path.dirname(p)
            target = os.path.join(folder, new_name)
            stem, ext = os.path.splitext(new_name)
            k = 1
            # renames run one by one: never take a name another file still has
            while (os.path.exists(target) and os.path.normcase(target) != os.path.normcase(p)) \
                    or os.path.normcase(target) in used:
                target = os.path.join(folder, f"{stem} ({k}){ext}")
                k += 1
            used.add(os.path.normcase(target))
            if os.path.normcase(target) != os.path.normcase(p):
                out.append((p, target))
        return out

    def _undo(self, app, s, spec):
        from ..tools.tasks import TaskFailed
        j = spec["journal"]
        op, pairs = j.get("op"), j.get("pairs", [])

        def work():
            ok = 0
            if op == "create":  # only what is still exactly as created: never your edits
                untouched, folders = [], []
                for p, size, mtime in j.get("created", []):
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    if os.path.isdir(p):
                        if not os.listdir(p):
                            folders.append(p)
                    elif st.st_size == size and abs(st.st_mtime - mtime) < 2:
                        untouched.append(p)
                items = untouched + folders
                return len(items) if to_recycle_bin(items) else 0
            if op == "copy":
                copies = [d for _s, d in pairs if os.path.exists(d)]
                return len(copies) if to_recycle_bin(copies) else 0
            for src, dst in pairs:
                try:
                    if os.path.exists(dst) and not os.path.exists(src):
                        os.makedirs(os.path.dirname(src), exist_ok=True)
                        shutil.move(dst, src)
                        ok += 1
                except OSError:
                    pass
            return ok
        box = background(work)
        yield ("until", lambda: box.get("done", False), 600.0)
        n = box.get("value") or 0
        if n == 0:
            raise TaskFailed(tr("fm.undo_none"))
        self._save_journal(app, {})
        s.jump()
        spec["done"] = tr("fm.undone_create" if op == "create" else "fm.undone", n=n)
