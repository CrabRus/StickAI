"""Checks that a "commented" version of code changed nothing but comments.

The model is told to only add comments / docstrings, but models slip. Before the user
sees the diff we compare the two versions line by line. Allowed:
  * new lines that are blank, comments or (doc)strings standing on their own,
  * a comment appended to the end of an existing line (`x = 1  # why`),
  * existing comment lines edited or removed.
Anything else (code changed, reordered, re-indented) is reported, and the panel warns.
This is a heuristic for many languages, not a parser: it errs on the side of warning.
"""
from __future__ import annotations

import difflib
import re

FENCE = re.compile(r"```[^\n`]*\n(.*?)(?:\n```|\Z)", re.S)
# a line that is only a comment (or a docstring line starting with quotes)
COMMENT_LINE = re.compile(r"""^\s*(?:#|//|/\*|\*|--|;|%|<!--|-->|'(?!')|rem\s|REM\s|[rRuUbB]?(?:\"\"\"|'''))""")
# what may follow existing code on the same line: whitespace + a comment
COMMENT_TAIL = re.compile(r"""^\s+(?:#|//|/\*|--|;|<!--|')""")


def extract_code(reply: str) -> str:
    """The code from a reply (inside the first ``` fence, or the whole reply)."""
    m = FENCE.search(reply)
    code = m.group(1) if m else reply.strip("\n")
    return code.rstrip("\n")


def _block_lines(lines: list[str]) -> set[int]:
    """Indices of lines inside triple-quoted strings / block comments that start a line
    (docstrings, /* ... */), which count as comments."""
    out: set[int] = set()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        quote = next((q for q in ('"""', "'''") if s.lstrip("rRuUbB").startswith(q)), None)
        if quote:
            body = s.lstrip("rRuUbB")[3:]
            out.add(i)
            if quote not in body:  # not closed on the same line: until the closing quotes
                i += 1
                while i < len(lines):
                    out.add(i)
                    if quote in lines[i]:
                        break
                    i += 1
        elif s.startswith("/*"):
            out.add(i)
            if "*/" not in s[2:]:
                i += 1
                while i < len(lines):
                    out.add(i)
                    if "*/" in lines[i]:
                        break
                    i += 1
        i += 1
    return out


def only_comments_added(original: str, proposed: str) -> bool:
    a = [ln.rstrip() for ln in original.split("\n")]
    b = [ln.rstrip() for ln in proposed.split("\n")]
    a_doc, b_doc = _block_lines(a), _block_lines(b)

    def a_free(i: int) -> bool:   # may be dropped / edited
        return not a[i].strip() or i in a_doc or bool(COMMENT_LINE.match(a[i]))

    def b_free(j: int) -> bool:   # may be added
        return not b[j].strip() or j in b_doc or bool(COMMENT_LINE.match(b[j]))

    def same_code(i: int, j: int) -> bool:
        if a[i] == b[j]:
            return True
        return bool(a[i].strip()) and b[j].startswith(a[i]) and bool(COMMENT_TAIL.match(b[j][len(a[i]):]))

    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        i, j = i1, j1
        while j < j2:
            if i < i2 and same_code(i, j):
                i, j = i + 1, j + 1
            elif b_free(j):
                if i < i2 and a_free(i) and not same_code(i, j):
                    i += 1  # an old comment replaced by a new one
                j += 1
            elif i < i2 and a_free(i):
                i += 1      # an old comment / blank line removed
            else:
                return False
        if any(not a_free(k) for k in range(i, i2)):
            return False    # code lines removed
    return True
