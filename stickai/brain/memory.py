"""Persistent memory, kept in the memory/ folder next to characters.toml.

  chat_log.jsonl   every message ever (you, them, notable events), one JSON
                   object per line. The newest lines are fed back to Gemini
                   as short-term memory, so a restart continues the chat.
  memory.json      long-term memory: facts Gemini distilled from older chat
                   (shared facts about you + notes per character). It is plain,
                   readable JSON; edit it if they remember something wrong.
  images/          copies of the pictures you sent, shown in the chat window.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger("stickai.memory")


@dataclass
class Entry:
    kind: str                 # "user" | "char" | "event" | "system"
    text: str
    speaker: str = ""         # character key for kind == "char"
    to: str = ""              # "user", "everyone" or a character key
    image: str = ""           # path relative to the memory dir
    image_note: str = ""      # Gemini's one-line description of the picture
    ts: float = field(default_factory=time.time)
    session: str = ""


class MemoryStore:
    def __init__(self, root: Path):
        self.root = root
        self.images = root / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.log_path = root / "chat_log.jsonl"
        self.facts_path = root / "memory.json"
        self.session = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.entries: list[Entry] = self._load_log()
        self.facts = self._load_facts()
        self.previous_seen = float(self.facts.get("last_seen", 0) or 0)

    # --------------------------------------------------------------- log
    def _load_log(self) -> list[Entry]:
        out = []
        if self.log_path.exists():
            for n, line in enumerate(self.log_path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    if d.get("kind") == "annotation":
                        for e in reversed(out):
                            if e.ts == d.get("ref_ts"):
                                e.image_note = d.get("image_note", "")
                                break
                        continue
                    out.append(Entry(**{k: v for k, v in d.items() if k in Entry.__dataclass_fields__}))
                except (ValueError, TypeError):
                    log.warning("chat_log.jsonl line %d is damaged; skipped", n)
        return out

    def add(self, entry: Entry) -> Entry:
        entry.session = entry.session or self.session
        self.entries.append(entry)
        self._append(asdict(entry))
        return entry

    def annotate_image(self, entry: Entry, note: str) -> None:
        """Attach Gemini's description of a picture to an already-logged entry."""
        entry.image_note = note
        self._append({"kind": "annotation", "ref_ts": entry.ts, "image_note": note})

    def _append(self, record: dict) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def save_image(self, png_bytes: bytes) -> str:
        name = f"{datetime.now():%Y%m%d-%H%M%S-%f}.png"
        (self.images / name).write_bytes(png_bytes)
        return f"images/{name}"

    def recent(self, n: int) -> list[Entry]:
        talk = [e for e in self.entries if e.kind != "system"]
        return talk[-n:] if n else []

    # ------------------------------------------------------------- facts
    def _load_facts(self) -> dict:
        try:
            d = json.loads(self.facts_path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
        except FileNotFoundError:
            pass
        except ValueError:
            log.warning("memory.json is not valid JSON; starting a fresh one (old file kept as .bak)")
            self.facts_path.replace(self.facts_path.with_suffix(".json.bak"))
        return {"shared": [], "characters": {}, "summarized_upto": 0, "last_seen": 0}

    def save_facts(self) -> None:
        tmp = self.facts_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.facts, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.facts_path)

    def touch(self) -> None:
        """Remember when we were last running (so they know how long you were gone)."""
        self.facts["last_seen"] = time.time()
        self.save_facts()

    @property
    def unsummarized(self) -> list[Entry]:
        talk = [e for e in self.entries if e.kind != "system"]
        return talk[int(self.facts.get("summarized_upto", 0)):]

    def apply_summary(self, shared: list[str], per_char: dict[str, list[str]], upto: int) -> None:
        self.facts["shared"] = shared
        self.facts["characters"] = per_char
        self.facts["summarized_upto"] = upto
        self.save_facts()

    def forget(self) -> None:
        """Wipe chat log, long-term memory and saved pictures (user's request)."""
        self.entries.clear()
        self.log_path.unlink(missing_ok=True)
        for f in self.images.glob("*"):
            f.unlink(missing_ok=True)
        self.facts = {"shared": [], "characters": {}, "summarized_upto": 0, "last_seen": 0}
        self.previous_seen = 0.0
        self.save_facts()
        log.info("Memory wiped")

    def reload_facts(self) -> None:
        self.facts = self._load_facts()
