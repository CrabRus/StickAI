"""Skills: what the stickmen can do, one module per area (see base.py).

The registry below feeds three places:
* the director (step kinds, schema fields, prompt rules + examples, context lines),
* the TaskRunner (resolve / run steps that belong to a skill),
* quick commands: messages understood locally, with no AI request at all.
"""
from __future__ import annotations

import re

from .base import Action, Param, Skill, Unusable  # noqa: F401
from .explorer import ExplorerSkill
from .filemanager import FileManagerSkill
from .system import SystemSkill
from .terminal import TerminalSkill

# order matters for quick(): the first skill that understands a message gets it
SKILLS: list[Skill] = [ExplorerSkill(), FileManagerSkill(), TerminalSkill(), SystemSkill()]
_OWNER = {kind: sk for sk in SKILLS for kind in sk.KINDS}

FILLER = re.compile(r"^(?:(?:hey|hi|эй|слушай|ну|а|ahoj|hej|можешь|можете|could you|can you|please|пожалуйста|"
                    r"prosím)[,!]?\s+)+", re.I)


def owner(kind: str) -> Skill | None:
    return _OWNER.get(kind)


def enabled(settings) -> list[Skill]:
    return [sk for sk in SKILLS if sk.enabled(settings)]


def kinds(settings) -> list[str]:
    return [a.kind for sk in enabled(settings) for a in sk.actions(settings)]


def schema_props(settings) -> dict:
    """Extra step fields the skills need (merged into the step schema)."""
    props: dict = {}
    for sk in enabled(settings):
        for a in sk.actions(settings):
            for name, p in a.params.items():
                if name in props:
                    continue
                d = {"type": p.type, "description": f"{a.kind}: {p.description}"}
                if p.enum:
                    d["enum"] = list(p.enum)
                props[name] = d
    return props


def rules(settings) -> str:
    out = []
    for sk in enabled(settings):
        for a in sk.actions(settings):
            params = ", ".join(a.params)
            out.append(f"\n  * {a.kind} ({params}): {a.doc}")
            out += [f"\n      e.g. {ex}" for ex in a.examples]
        extra = sk.rules(settings)
        if extra:
            out.append("\n  " + extra)
    return "".join(out)


def context(app) -> list[str]:
    lines = []
    for sk in enabled(app.roster.settings):
        lines += sk.context(app)
    return lines


def quick(text: str, app) -> list[dict] | None:
    """Steps for a message that is just a common command, or None."""
    t = FILLER.sub("", " ".join(text.strip().split()))
    if not t:
        return None
    for sk in enabled(app.roster.settings):
        try:
            steps = sk.quick(t, app)
        except Exception:  # noqa: BLE001 - a quick command must never break the chat
            steps = None
        if steps:
            return steps
    return None
