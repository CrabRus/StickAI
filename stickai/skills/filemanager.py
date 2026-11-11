"""Files: move / copy / delete / list YOUR files by type, name and date.

"перенеси все pdf за сегодня из загрузок в документы", "copy today's photos from Downloads to Pictures\\Trip",
"delete the .tmp files in Downloads", "какие архивы я скачал на этой неделе?", "отмени" (undo).

Safety:
* the Allow dialog shows how many files and which (list never asks);
* delete = to the Recycle Bin only (always asks);
* nothing is overwritten: a clash becomes "name (1).pdf";
* at most MAX_FILES per operation, hidden / system files are skipped;
* the last move / copy is journaled (memory/file_ops.json) so "undo" can put it back.
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
from .explorer import _index, known_folder

MAX_FILES = 500
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


def _short(paths: list[str], n: int = 8) -> str:
    names = [os.path.basename(p) for p in paths[:n]]
    return ", ".join(names) + (f" … (+{len(paths) - n})" if len(paths) > n else "")


class FileManagerSkill(Skill):
    name = "files"
    KINDS = ("manage_files", "undo_files")

    def __init__(self):
        self.journal_path: Path | None = None

    def actions(self, settings):
        return [
            Action("manage_files",
                   "move / copy / delete (to the Recycle Bin) / list the user's files in a folder, filtered by type, "
                   "name and date. Shows the user exactly which files before doing it.",
                   {"op": Param("STRING", "what to do", ("move", "copy", "delete", "list")),
                    "from": Param("STRING", "source folder: downloads, documents, desktop, pictures, music, videos, "
                                            "a full path, or a folder name"),
                    "to": Param("STRING", "destination for move / copy; may be a NEW subfolder like 'documents/PDF'"),
                    "types": Param("STRING", "extensions or categories, e.g. 'pdf' or 'jpg,png' or 'images' / "
                                             "'documents' / 'video' / 'audio' / 'archives'; empty = all"),
                    "name": Param("STRING", "only files whose name contains this"),
                    "when": Param("STRING", "by date (downloaded / created / changed)", WHEN),
                    "newest": Param("INTEGER", "only the N newest (e.g. 1 = the last downloaded file)"),
                    "subfolders": Param("BOOLEAN", "include subfolders")},
                   ('"move all today\'s pdfs from downloads to documents" -> {"kind":"manage_files","op":"move",'
                    '"from":"downloads","to":"documents","types":"pdf","when":"today"}',
                    '"what did I download this week?" -> {"kind":"manage_files","op":"list","from":"downloads",'
                    '"when":"this_week"}')),
            Action("undo_files", "undo the last move / copy (puts the files back / removes the copies)", {}),
        ]

    def rules(self, settings):
        return ("For list, add \"continue\": true and tell the user what you found. Never delete unless the user "
                "clearly asked to delete.")

    def quick(self, text, app):
        if UNDO_QUICK.match(text) and self._journal(app).get("pairs"):
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
            if not j.get("pairs"):
                raise Unusable("nothing to undo")
            n = len(j["pairs"])
            return {"kind": "undo_files", "skill": self.name, "journal": j, "trusted": True,
                    "what": tr("fm.undo", n=n, op=tr("fm.op_" + j.get("op", "move")))}
        op = str(step.get("op", "list")).lower()
        if op not in ("move", "copy", "delete", "list"):
            raise Unusable("op must be move, copy, delete or list")
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
        s.gesture, s.gesture_until = "type", s.time + 2.0

        def work():
            pairs, failed = [], []
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
        s.gesture = ""
        pairs, failed = box.get("value") or ([], files)
        if op in ("move", "copy") and pairs:
            self._save_journal(app, {"op": op, "pairs": pairs, "time": time.time()})
        done = len(files) - len(failed)
        if done == 0:
            raise TaskFailed(tr("fm.failed_all"))
        s.jump()
        spec["done"] = tr("fm.done_" + op, n=done, dst=spec["dst"] or "") + \
            (tr("fm.some_failed", n=len(failed)) if failed else "")
        spec["report"] = spec["done"] + (f"; not done (in use?): {_short(failed)}" if failed else "")

    def _undo(self, app, s, spec):
        from ..tools.tasks import TaskFailed
        j = spec["journal"]
        op, pairs = j.get("op"), j.get("pairs", [])

        def work():
            ok = 0
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
        spec["done"] = tr("fm.undone", n=n)
