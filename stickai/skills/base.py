"""What every skill looks like.

A skill teaches the stickmen one area (the system, Explorer, the terminal...):

* actions   - step kinds the model may use, with their parameters (+ a one-line
              description and a few examples, which small models follow far better
              than rules);
* quick()   - common commands understood locally, without any AI request
              ("пауза", "открой загрузки", "remind me in 10 minutes to ...");
* resolve() - turns a step from the model into a concrete spec, or raises
              Unusable(reason) with words the model can act on;
* run()     - a generator stepped by the TaskRunner (same commands as tasks.py:
              ("wait", s), ("until", cond, timeout), ("glide", x, y, s), ("ask", ...));
              it raises TaskFailed on failure and may set spec["report"] (what the
              model should know afterwards) and spec["followup"] (look again).

Everything a skill does still goes through the plan / Allow / feedback machinery.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field


class Unusable(Exception):
    """A step that can't run as given; the message tells the model how to fix it."""


@dataclass
class Param:
    type: str               # "STRING" | "INTEGER" | "NUMBER" | "BOOLEAN"
    description: str
    enum: tuple[str, ...] = ()


@dataclass
class Action:
    kind: str
    doc: str                                  # one line for the prompt
    params: dict[str, Param] = field(default_factory=dict)
    examples: tuple[str, ...] = ()            # 'user: "..." -> {"kind": ...}'


class Skill:
    name = "skill"

    def enabled(self, settings) -> bool:
        return bool(settings.control)

    def actions(self, settings) -> list[Action]:
        return []

    def rules(self, settings) -> str:
        """Extra guidance for the prompt (short)."""
        return ""

    def context(self, app) -> list[str]:
        """Lines for the prompt about the current state (e.g. the terminal's folder)."""
        return []

    def quick(self, text: str, app) -> list[dict] | None:
        """Steps for a command understood locally, or None."""
        return None

    def resolve(self, runner, step: dict, targets: dict) -> dict:
        raise Unusable("not supported")

    def run(self, runner, s, spec: dict):
        if False:  # pragma: no cover - makes this a generator
            yield
        return


# ---------------------------------------------------------------- helpers for skills
def background(fn) -> dict:
    """Run fn on a thread; poll box["done"] / read box["value"] (UI Automation, searches...)."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001 - reported by the caller
            box["error"] = e
            box["value"] = None
        box["done"] = True
    threading.Thread(target=run, daemon=True).start()
    return box


def words_re(*alternatives: str) -> re.Pattern:
    """Case-insensitive whole-phrase alternation."""
    return re.compile(r"^(?:%s)$" % "|".join(alternatives), re.IGNORECASE)
