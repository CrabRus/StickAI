"""Streaming text replies (Markdown) for the code helper.

Unlike the chat (one JSON answer), explanations are plain text shown while it's
being written. Runs on a worker thread; pieces arrive on the Qt thread via signals.
Keys and the model order come from the same places as the chat (gemini.py).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..brain.gemini import find_api_keys

log = logging.getLogger("stickai.code")

GEMINI_STREAM = "https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"
CONNECT_TIMEOUT_S = 25      # first byte; afterwards each read may wait this long too
NEXT_MODEL = {403, 404, 429, 500, 502, 503, 504}
REST_S = {429: 600, 403: 3600, 404: 3600}   # a model with no quota / no access: skip it for a while


class StreamError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _error_code(e: Exception) -> str:
    """Map to message codes the UI translates (i18n err.*)."""
    if isinstance(e, StreamError):
        if e.status == 429:
            return "err.quota"
        if e.status in (500, 502, 503, 504):
            return "err.overloaded"
        if e.status in (401, 403) or "API key" in str(e):
            return "err.bad_key"
        if e.status == 404:
            return "err.no_model"
        return f"err.other:{e}"
    if isinstance(e, TimeoutError):
        return "err.timeout"
    if isinstance(e, (urllib.error.URLError, OSError)):
        return "err.offline"
    return f"err.other:{e}"


class TextStream(QObject):
    chunk = Signal(int, str)        # request id, new text
    finished = Signal(int, str, str)  # request id, error code ("" = ok), model used

    def __init__(self, secrets_path: Path):
        super().__init__()
        self.secrets_path = secrets_path
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="codestream")
        self._next = 0
        self._cancel: dict[int, threading.Event] = {}
        self._rest: dict[tuple[str, str], float] = {}   # (key, model) -> monotonic time to try again

    def has_key(self, owner: str | None = None) -> bool:
        return bool(find_api_keys(self.secrets_path, owner))

    def start(self, models: tuple[str, ...], system: str, prompt: str, temperature: float = 0.4,
              owner: str | None = None) -> int:
        self._next += 1
        rid = self._next
        self._cancel[rid] = threading.Event()
        self._pool.submit(self._run, rid, models, system, prompt, temperature, owner)
        return rid

    def cancel(self, rid: int) -> None:
        ev = self._cancel.get(rid)
        if ev is not None:
            ev.set()

    def shutdown(self) -> None:
        for ev in self._cancel.values():
            ev.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ worker
    def _run(self, rid, models, system, prompt, temperature, owner) -> None:
        stop = self._cancel[rid]
        err, used = "err.no_key", ""
        try:
            keys = find_api_keys(self.secrets_path, owner)
            for key in keys:
                now = time.monotonic()
                usable = [m for m in models if self._rest.get((key, m), 0) <= now] or list(models)
                for model in usable:
                    if stop.is_set():
                        return
                    sent = [False]
                    try:
                        self._gemini(key, model, system, prompt, temperature, stop, rid, sent)
                        err, used = "", model
                        return
                    except Exception as e:  # noqa: BLE001 - reported to the user below
                        err = _error_code(e)
                        log.warning("code stream %s: %s", model, e)
                        if isinstance(e, StreamError) and e.status in REST_S:
                            self._rest[(key, model)] = time.monotonic() + REST_S[e.status]
                        if sent[0]:
                            return  # half an answer is on screen: don't start another one under it
                        if not (isinstance(e, StreamError) and e.status in NEXT_MODEL) \
                                and not isinstance(e, TimeoutError):
                            if err != "err.bad_key":
                                return
                            break  # this key is bad: next key
        finally:
            self._cancel.pop(rid, None)
            self.finished.emit(rid, "" if stop.is_set() else err, used)

    def _gemini(self, key, model, system, prompt, temperature, stop, rid, sent) -> None:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        req = urllib.request.Request(
            GEMINI_STREAM.format(model=model), data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            resp = urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT_S)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
            except (ValueError, OSError):
                msg = ""
            raise StreamError(f"HTTP {e.code} {msg[:200]}".strip(), e.code) from None
        with resp:
            for raw in resp:
                if stop.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    data = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if data.get("promptFeedback", {}).get("blockReason"):
                    raise StreamError(f"blocked: {data['promptFeedback']['blockReason']}")
                for cand in data.get("candidates") or []:
                    for part in cand.get("content", {}).get("parts", []):
                        text = part.get("text", "")
                        if text and not part.get("thought"):
                            sent[0] = True
                            self.chunk.emit(rid, text)
        if not sent[0]:
            raise StreamError("empty response")
