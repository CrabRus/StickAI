"""characters.toml: global settings plus one sheet per stickman.

`load_roster` parses and validates it; `save_roster` writes it back (the
settings window uses this), regenerating the explanatory comments.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

LANGUAGES = {"en": "English", "ru": "Russian", "sk": "Slovak"}
# The sheet as shipped; "Reset to defaults" in the settings window restores from it.
DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "defaults" / "characters.default.toml"
BUBBLE_MODES = ("full", "indicator", "off")
VISION_MODES = ("off", "ask", "auto")


@dataclass(frozen=True)
class CharacterSpec:
    key: str
    display_name: str
    aliases: tuple[str, ...]
    color: str
    size: float = 1.0
    walk_speed: float = 0.95
    energy: float = 1.0
    climb_chance: float = 0.85
    restlessness: float = 1.0
    personality: str = ""
    voice: str = ""

    @property
    def names(self) -> tuple[str, ...]:
        """Every word that refers to this character (lower-case)."""
        return tuple({self.key.lower(), self.display_name.lower(), *(a.lower() for a in self.aliases)})


@dataclass(frozen=True)
class Settings:
    language: str = "en"                 # en | ru | sk: what they speak and the UI language
    bubbles: str = "full"                # full | indicator (! / ?) | off (chat window only)
    hotkey: str = "alt+`"
    hide_hotkey: str = "alt+shift+`"     # hide/show everything
    models: tuple[str, ...] = ("gemini-3.5-flash-lite", "gemini-3.8-flash")
    temperature: float = 0.95
    start_with: tuple[str, ...] = ()
    group_words: tuple[str, ...] = ("guys", "everyone", "everybody")
    max_replies: int = 3
    history_lines: int = 16
    share_window_titles: bool = False
    world: str = ""
    # characters chatting among themselves
    banter: bool = True
    banter_every: tuple[float, float] = (150.0, 360.0)   # seconds, random in range
    react_to_throws: bool = True
    greet_on_arrival: bool = True   # off: a summoned character drops in without saying anything
    # long-term memory
    summarize_every: int = 30
    memory_facts: int = 30
    chat_on_top: bool = True
    spellcheck: bool = True      # red underlines + suggestions in the chat input (Windows spell checker)
    widgets: bool = True     # walk/sit on search bars & fields inside windows
    web_surfaces: bool = True   # also headings / images / buttons of web pages
    vision: str = "off"         # off | ask (when you ask / they want to look) | auto (also on their own)
    control: bool = False       # may close/minimize/open windows and type, after you allow each action
    trust_windows: bool = False  # ...open/close/show/hide windows WITHOUT asking (typing still asks)
    read_fields: bool = False    # ...check spelling in other apps' text fields (reads that one field, locally)
    clipboard: bool = False      # read / replace what you copied, only when your message mentions it
    reminders: bool = True       # "remind me in 20 minutes" (local timers)
    apps: tuple[tuple[str, str], ...] = ()   # (name, path) of apps they may open
    files: bool = False          # may create folders / create, edit, open text files in files_folder
    files_folder: str = ""       # empty = Documents\StickAI
    trust_files: bool = False    # ...without asking
    music_file: str = ""         # mp3 for the music show
    music_bpm: int = 110         # beats per minute: how often the boxes are struck
    music_output: str = ""       # "" = Windows default, "auto-speakers", or an output device name
    couch: bool = True           # sit on a couch together when you watch videos


@dataclass(frozen=True)
class Roster:
    settings: Settings
    characters: dict[str, CharacterSpec] = field(default_factory=dict)

    def with_settings(self, **changes) -> "Roster":
        return Roster(replace(self.settings, **changes), dict(self.characters))

    def with_character(self, spec: CharacterSpec) -> "Roster":
        chars = dict(self.characters)
        chars[spec.key] = spec
        return Roster(self.settings, chars)


def _clamp(v, lo, hi, name):
    v = float(v)
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi} (got {v})")
    return v


def _range(v, name) -> tuple[float, float]:
    try:
        lo, hi = (float(v), float(v)) if isinstance(v, (int, float)) else (float(v[0]), float(v[1]))
    except (TypeError, ValueError, IndexError):
        raise ValueError(f"{name} must be a number of seconds or [min, max] (got {v!r})") from None
    if not 20 <= lo <= hi:
        raise ValueError(f"{name} must be at least 20 seconds and min <= max (got {v!r})")
    return lo, hi


def _prose(v) -> str:
    """Join hard-wrapped lines into flowing paragraphs (blank lines stay breaks)."""
    paras = [" ".join(line.strip() for line in p.splitlines() if line.strip())
             for p in re.split(r"\n\s*\n", str(v).strip())]
    return "\n\n".join(p for p in paras if p)


def _choice(v, options, name) -> str:
    v = str(v).lower().strip()
    if v not in options:
        raise ValueError(f"{name} must be one of {', '.join(options)} (got {v!r})")
    return v


def load_roster(path: Path) -> Roster:
    """Parse and validate. Raises ValueError with a readable message."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))  # utf-8-sig: tolerate a BOM (Notepad)
    except FileNotFoundError:
        raise ValueError(f"{path.name} not found") from None
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path.name}: {e}") from None

    s = data.get("settings", {})
    d = Settings()
    models = s.get("models", list(d.models))
    if isinstance(models, str):
        models = [models]
    settings = Settings(
        language=_choice(s.get("language", d.language), LANGUAGES, "language"),
        bubbles=_choice(s.get("bubbles", d.bubbles), BUBBLE_MODES, "bubbles"),
        hotkey=str(s.get("hotkey", d.hotkey)),
        hide_hotkey=str(s.get("hide_hotkey", d.hide_hotkey)),
        models=tuple(str(m) for m in models) or d.models,
        temperature=_clamp(s.get("temperature", d.temperature), 0, 2, "temperature"),
        start_with=tuple(str(k).lower() for k in s.get("start_with", [])),
        group_words=tuple(str(w).lower() for w in s.get("group_words", d.group_words)),
        max_replies=int(_clamp(s.get("max_replies", d.max_replies), 1, 8, "max_replies")),
        history_lines=int(_clamp(s.get("history_lines", d.history_lines), 0, 100, "history_lines")),
        share_window_titles=bool(s.get("share_window_titles", False)),
        world=_prose(s.get("world", "")),
        banter=bool(s.get("banter", d.banter)),
        banter_every=_range(s.get("banter_every", list(d.banter_every)), "banter_every"),
        react_to_throws=bool(s.get("react_to_throws", d.react_to_throws)),
        greet_on_arrival=bool(s.get("greet_on_arrival", d.greet_on_arrival)),
        summarize_every=int(_clamp(s.get("summarize_every", d.summarize_every), 6, 500, "summarize_every")),
        memory_facts=int(_clamp(s.get("memory_facts", d.memory_facts), 5, 200, "memory_facts")),
        chat_on_top=bool(s.get("chat_on_top", d.chat_on_top)),
        spellcheck=bool(s.get("spellcheck", d.spellcheck)),
        widgets=bool(s.get("widgets", d.widgets)),
        web_surfaces=bool(s.get("web_surfaces", d.web_surfaces)),
        vision=_choice(s.get("vision", d.vision), VISION_MODES, "vision"),
        control=bool(s.get("control", d.control)),
        trust_windows=bool(s.get("trust_windows", d.trust_windows)),
        read_fields=bool(s.get("read_fields", d.read_fields)),
        clipboard=bool(s.get("clipboard", d.clipboard)),
        reminders=bool(s.get("reminders", d.reminders)),
        apps=_apps(s.get("apps", {})),
        files=bool(s.get("files", d.files)),
        files_folder=str(s.get("files_folder", d.files_folder)),
        trust_files=bool(s.get("trust_files", d.trust_files)),
        music_file=str(s.get("music_file", d.music_file)),
        music_bpm=int(_clamp(s.get("music_bpm", d.music_bpm), 40, 240, "music_bpm")),
        music_output=str(s.get("music_output", d.music_output)),
        couch=bool(s.get("couch", d.couch)),
    )

    chars: dict[str, CharacterSpec] = {}
    for key, c in data.get("characters", {}).items():
        key = key.lower()
        where = f"[characters.{key}]"
        color = str(c.get("color", "")).strip()
        if not (color.startswith("#") and len(color) in (4, 7)):
            raise ValueError(f"{where} color must look like \"#FEC503\" (got {color!r})")
        chars[key] = CharacterSpec(
            key=key,
            display_name=str(c.get("display_name", key.title())),
            aliases=tuple(str(a) for a in c.get("aliases", [])),
            color=color,
            size=_clamp(c.get("size", 1.0), 0.3, 3.0, f"{where} size"),
            walk_speed=_clamp(c.get("walk_speed", 0.95), 0.1, 4.0, f"{where} walk_speed"),
            energy=_clamp(c.get("energy", 1.0), 0.0, 2.0, f"{where} energy"),
            climb_chance=_clamp(c.get("climb_chance", 0.85), 0.0, 1.0, f"{where} climb_chance"),
            restlessness=_clamp(c.get("restlessness", 1.0), 0.0, 10.0, f"{where} restlessness"),
            personality=_prose(c.get("personality", "")),
            voice=_prose(c.get("voice", "")),
        )
    if not chars:
        raise ValueError(f"{path.name} defines no [characters.*] tables")
    unknown = [k for k in settings.start_with if k not in chars]
    if unknown:
        raise ValueError(f"start_with names unknown characters: {', '.join(unknown)}")
    return Roster(settings, chars)


def load_defaults() -> Roster | None:
    try:
        return load_roster(DEFAULTS_PATH)
    except ValueError:
        return None


# ---------------------------------------------------------------- writing
def _str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)  # JSON strings are valid TOML basic strings


def _ml(v: str) -> str:
    v = v.replace("\\", "\\\\").replace('"""', '\\"""')
    return f'"""\n{v.strip()}\n"""'


def _apps(v) -> tuple[tuple[str, str], ...]:
    if not isinstance(v, dict):
        raise ValueError('apps must be a table like apps = { "Spotify" = "C:/.../Spotify.exe" }')
    return tuple((str(k).strip(), str(p).strip()) for k, p in v.items() if str(k).strip() and str(p).strip())


def _list(vs) -> str:
    return "[" + ", ".join(_str(str(x)) for x in vs) + "]"


def _num(v: float) -> str:
    return f"{v:g}" if v != int(v) else f"{v:.1f}"


def save_roster(path: Path, roster: Roster) -> None:
    """Write the roster back as a commented, human-friendly TOML file."""
    s = roster.settings
    out = [
        "# " + "─" * 77,
        "#  StickAI character sheet",
        "#  Edit here or in the app (chat window → ⚙ Settings). Changes apply on save.",
        "# " + "─" * 77,
        "",
        "[settings]",
        "# Language they speak (and the app's language): en, ru, sk",
        f"language = {_str(s.language)}",
        "",
        "# What appears above their heads when they talk:",
        '#   "full" = speech bubbles, "indicator" = just ! or ?, "off" = chat window only',
        f"bubbles = {_str(s.bubbles)}",
        "",
        "# Global hotkey that shows/hides the chat window. Modifiers: ctrl, alt, shift, win.",
        "# Keys: a-z, 0-9, space, enter, tab, f1-f12, `, /, ;, ', [, ], -, =",
        f"hotkey = {_str(s.hotkey)}",
        "# Hotkey that hides / shows the stickmen and all their windows at once.",
        f"hide_hotkey = {_str(s.hide_hotkey)}",
        "",
        "# Gemini models, tried in order (the next is used if one is rate-limited,",
        "# overloaded or unavailable). Flash-Lite has a much bigger free daily quota.",
        f"models = {_list(s.models)}",
        f"temperature = {_num(s.temperature)}",
        "",
        "# Who is on the desktop when the app starts, e.g. [\"yellow\"].",
        f"start_with = {_list(s.start_with)}",
        "",
        "# Words that summon / address everybody at once.",
        f"group_words = {_list(s.group_words)}",
        "",
        "# How many replies one message can produce, and how much recent chat is sent.",
        f"max_replies = {s.max_replies}",
        f"history_lines = {s.history_lines}",
        "",
        "# When two or more are out and you're quiet, they chat among themselves every",
        "# banter_every = [min, max] seconds (each chat costs one Gemini request).",
        f"banter = {str(s.banter).lower()}",
        f"banter_every = [{_num(s.banter_every[0])}, {_num(s.banter_every[1])}]",
        "",
        "# Comment when you grab one and fling them across the screen.",
        f"react_to_throws = {str(s.react_to_throws).lower()}",
        "",
        "# Say something when called onto the desktop. false = they drop in silently and only",
        "# answer if your message asked them something (a bare \"yellow\" or \"hi guys\" gets no reply).",
        f"greet_on_arrival = {str(s.greet_on_arrival).lower()}",
        "",
        "# Long-term memory (memory/ folder): every `summarize_every` new chat lines are",
        "# folded into memory/memory.json, kept to about `memory_facts` facts.",
        f"summarize_every = {s.summarize_every}",
        f"memory_facts = {s.memory_facts}",
        "",
        "# Let them walk, sit and hop onto search bars and text fields inside windows",
        "# (found with Windows UI Automation; only their position is read, never text).",
        f"widgets = {str(s.widgets).lower()}",
        "# ...including headings, pictures and buttons of web pages (read from the page's",
        "# accessibility tree, i.e. its HTML structure).",
        f"web_surfaces = {str(s.web_surfaces).lower()}",
        "",
        "# Let them see your screen (screenshots are sent to Google Gemini):",
        '#   "off", "ask" = when you ask or they want to look, "auto" = also now and then on their own',
        f"vision = {_str(s.vision)}",
        "",
        "# Let them close / minimize / open your windows and type into fields when you ask.",
        "# Every single action still asks for your permission first. Sends window titles to Google.",
        f"control = {str(s.control).lower()}",
        "# With control on: open / close / show / hide windows WITHOUT asking first",
        "# (only when you ask for it in the chat; typing text still asks every time).",
        f"trust_windows = {str(s.trust_windows).lower()}",
        "# With control on: check spelling in a text field of another app when you ask. Reads only",
        "# that field's text, checked on your PC by the Windows spell checker (never sent anywhere).",
        f"read_fields = {str(s.read_fields).lower()}",
        "",
        "# Read and replace what you copied (\"translate what I copied\"), only when your message",
        "# mentions it. The copied text is sent to Google with that message.",
        f"clipboard = {str(s.clipboard).lower()}",
        "",
        "# Timers and reminders (\"remind me in 20 minutes to stretch\"). Counted down on your PC.",
        f"reminders = {str(s.reminders).lower()}",
        "",
        "# Apps they may open when you ask (\"open Spotify\"): name = program, shortcut or file.",
        "apps = {" + ", ".join(f"{_str(n)} = {_str(p)}" for n, p in s.apps) + "}",
        "",
        "# Their own folder: create folders and create / edit / open text files ONLY in there",
        "# (empty = Documents\\StickAI). Old versions are backed up before edits. File names and the",
        "# text of a few recent files are sent to Gemini so they can edit them.",
        f"files = {str(s.files).lower()}",
        f"files_folder = {_str(s.files_folder)}",
        f"trust_files = {str(s.trust_files).lower()}",
        "",
        "# Music show (\"Green, play some music\"): the mp3 to play and its tempo.",
        f"music_file = {_str(s.music_file)}",
        f"music_bpm = {s.music_bpm}",
        "# Where the song plays: \"\" = Windows default, \"auto-speakers\" = speakers whenever",
        "# headphones are the default output, or an exact device name from Settings.",
        f"music_output = {_str(s.music_output)}",
        "",
        "# When you watch something (fullscreen, YouTube, Netflix, VLC...), they gather on a couch.",
        f"couch = {str(s.couch).lower()}",
        "",
        "# Keep the chat window above other windows.",
        f"chat_on_top = {str(s.chat_on_top).lower()}",
        "",
        "# Underline misspelled words in the chat box (uses the Windows spell checker and its languages).",
        f"spellcheck = {str(s.spellcheck).lower()}",
        "",
        "# Tell them which window each one is standing on (sends window titles to Google).",
        f"share_window_titles = {str(s.share_window_titles).lower()}",
        "",
        "# Shared background every character knows.",
        f"world = {_ml(s.world)}",
        "",
        "# " + "─" * 77,
        "#  Characters. [characters.<key>]: the key is what you type to call them.",
        "#  size 1.0 = default height · walk_speed body-heights/s · energy 0..2 (higher runs",
        "#  and jumps more, lower sits around more) · climb_chance 0..1 · restlessness",
        "#  (how often they stop to look around) · personality & voice are sent to Gemini.",
        "# " + "─" * 77,
    ]
    for c in roster.characters.values():
        out += [
            "",
            f"[characters.{c.key}]",
            f"display_name = {_str(c.display_name)}",
            f"aliases = {_list(c.aliases)}",
            f"color = {_str(c.color)}",
            f"size = {_num(c.size)}",
            f"walk_speed = {_num(c.walk_speed)}",
            f"energy = {_num(c.energy)}",
            f"climb_chance = {_num(c.climb_chance)}",
            f"restlessness = {_num(c.restlessness)}",
            f"personality = {_ml(c.personality)}",
            f"voice = {_ml(c.voice)}",
        ]
    text = "\n".join(out) + "\n"
    load_check = tomllib.loads(text)  # never write a file we couldn't read back
    assert "settings" in load_check
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
