"""The stickmen's own folder on your PC (default: Documents\\StickAI).

They can create folders and create / edit / open text files, but only
inside this folder:
  * paths are relative to the folder and resolved; anything that would
    land outside it (.., absolute paths, other drives, links) is refused;
  * only plain-text kinds of files (no programs or scripts that could run
    when double-clicked);
  * before a file is overwritten, the old version is copied to
    <folder>\\.stickai-backups\\ so an edit can always be undone.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

TEXT_EXT = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".css", ".log", ".ini", ".yaml", ".yml",
            ".toml", ".rtf", ".tex", ".srt"}
MAX_BYTES = 100_000
BACKUPS = ".stickai-backups"


class WorkspaceError(Exception):
    pass


def default_folder() -> str:
    return str(Path.home() / "Documents" / "StickAI")


class Workspace:
    def __init__(self, folder: str):
        self.root = Path(os.path.expandvars(os.path.expanduser(folder or default_folder()))).resolve()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ safety
    def resolve(self, rel: str) -> Path:
        rel = str(rel or "").strip().replace("\\", "/").lstrip("/")
        if not rel or ":" in rel or rel.startswith("~"):
            raise WorkspaceError(f"not a path inside the folder: {rel!r}")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise WorkspaceError(f"outside the StickAI folder: {rel!r}")
        if BACKUPS in p.relative_to(self.root).parts:
            raise WorkspaceError("the backup folder is off-limits")
        return p

    def _check_file(self, p: Path) -> None:
        if p.suffix.lower() not in TEXT_EXT:
            raise WorkspaceError(f"only text files ({', '.join(sorted(TEXT_EXT))}), not {p.suffix or 'no extension'}")

    # ------------------------------------------------------------ actions
    def create_folder(self, rel: str) -> Path:
        self.ensure()
        p = self.resolve(rel)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write_file(self, rel: str, content: str) -> tuple[Path, bool]:
        """Create or overwrite a text file. Returns (path, existed_before)."""
        self.ensure()
        p = self.resolve(rel)
        self._check_file(p)
        data = content.encode("utf-8")
        if len(data) > MAX_BYTES:
            raise WorkspaceError(f"too big ({len(data)} bytes, max {MAX_BYTES})")
        existed = p.exists()
        if existed:
            if p.is_dir():
                raise WorkspaceError("that's a folder")
            bdir = self.root / BACKUPS
            bdir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(p, bdir / f"{stamp}_{'_'.join(p.relative_to(self.root).parts)}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p, existed

    def open(self, rel: str) -> Path:
        """Open a file (in its default app) or a folder (in Explorer)."""
        self.ensure()
        p = self.root if rel in ("", ".", "/") else self.resolve(rel)
        if not p.exists():
            raise WorkspaceError("doesn't exist")
        if p.is_file():
            self._check_file(p)
        os.startfile(p)
        return p

    # ----------------------------------------------------------- context
    def listing(self, max_entries: int = 40) -> list[str]:
        if not self.root.exists():
            return []
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d != BACKUPS)
            base = Path(dirpath).relative_to(self.root)
            for d in dirnames:
                out.append(f"{(base / d).as_posix()}/")
            for f in sorted(filenames):
                fp = Path(dirpath) / f
                out.append(f"{(base / f).as_posix()} ({fp.stat().st_size} bytes)")
            if len(out) >= max_entries:
                break
        return out[:max_entries]

    def recent_texts(self, n: int = 4, max_chars: int = 3000) -> list[tuple[str, str]]:
        """Contents of the most recently changed small text files."""
        if not self.root.exists():
            return []
        files = [p for p in self.root.rglob("*") if p.is_file() and p.suffix.lower() in TEXT_EXT
                 and BACKUPS not in p.relative_to(self.root).parts]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for p in files[:n]:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(text) > max_chars:
                text = text[:max_chars] + "\n…(truncated)"
            out.append((p.relative_to(self.root).as_posix(), text))
        return out
