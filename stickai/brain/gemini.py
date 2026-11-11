"""Minimal Gemini API client (REST, standard library only).

Requests run on a worker thread; results come back to the Qt main thread
through a signal, so the animation never stalls while Gemini thinks.

API keys, in the order they're tried:
  1. environment variable GEMINI_API_KEY (or GOOGLE_API_KEY)
  2. secrets.toml next to characters.toml:   gemini_api_key = "..."
  3. secrets.toml backups:                   backup_api_keys = ["...", "..."]
A backup key is used only when the one before it is out of quota, rejected
or has no access to any of the models (e.g. a paid key behind a free one).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

log = logging.getLogger("stickai.gemini")

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRY_NEXT_MODEL = {403, 404, 429, 500, 502, 503, 504}
UNAVAILABLE_SKIP_S = 3600  # a model with no quota / not available: don't ask it again for an hour
OVERLOADED = {500, 502, 503, 504}   # Google-side spikes: retry the same model first
OVERLOAD_RETRIES = 2
TIMEOUT_S = 20


def _friendly(e: "GeminiError") -> str:
    """Map API errors to message codes the UI translates (see i18n.py)."""
    if e.status in OVERLOADED:
        return "err.overloaded"
    if e.status == 429:
        return "err.quota"
    if e.status in (401, 403) or "API key" in str(e):
        return "err.bad_key"
    if e.status == 404:
        return "err.no_model"
    return f"err.other:{e}"


def _registry_env(var: str) -> str:
    """User-level env var straight from the registry: `setx` writes there, but
    processes started before it (e.g. an old terminal) don't see it."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, var)[0]).strip()
    except OSError:
        return ""


def find_api_keys(secrets_path: Path) -> list[str]:
    """Every configured key, primary first, without duplicates."""
    keys: list[str] = []
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.environ.get(var, "").strip() or _registry_env(var)
        if value:
            keys.append(value)
            break
    try:
        data = tomllib.loads(secrets_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        data = {}
    keys.append(str(data.get("gemini_api_key", "")).strip())
    backups = data.get("backup_api_keys", [])
    keys += [str(k).strip() for k in (backups if isinstance(backups, list) else [backups])]
    return list(dict.fromkeys(k for k in keys if k))


def find_api_key(secrets_path: Path) -> str | None:
    keys = find_api_keys(secrets_path)
    return keys[0] if keys else None


# errors where another key might help (no quota / no access / bad key); anything else won't
KEY_PROBLEMS = ("err.quota", "err.bad_key", "err.no_model")


@dataclass
class GeminiResult:
    request_id: int
    data: dict | None = None   # parsed JSON reply
    model: str = ""
    error: str = ""
    tag: str = ""              # what the request was for ("chat", "banter", "summary"...)


class GeminiError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < start:
        raise GeminiError("reply was not JSON")
    return json.loads(t[start:end + 1])


class GeminiClient(QObject):
    finished = Signal(object)  # GeminiResult, delivered on the main thread

    def __init__(self, secrets_path: Path):
        super().__init__()
        self._skip_until: dict[tuple[str, str], float] = {}  # (key, model) -> time.monotonic() to try again
        self.unavailable: set[str] = set()      # models that refused this session (for a one-time notice)
        self.secrets_path = secrets_path
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gemini")
        self._next_id = 0

    def has_key(self) -> bool:
        return find_api_key(self.secrets_path) is not None

    def generate(self, models: tuple[str, ...], system: str, prompt: str, schema: dict | None,
                 temperature: float, image: tuple[bytes, str] | None = None, tag: str = "") -> int:
        """Queue a request. `image` is (bytes, mime type) sent alongside the prompt."""
        self._next_id += 1
        rid = self._next_id
        self._pool.submit(self._run, rid, models, system, prompt, schema, temperature, image, tag)
        return rid

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ---------------------------------------------------------- worker side
    def _run(self, rid, models, system, prompt, schema, temperature, image=None, tag="") -> None:
        try:
            self._run_inner(rid, models, system, prompt, schema, temperature, image, tag)
        except Exception as e:  # never let a worker die silently
            log.exception("Gemini worker crashed")
            self.finished.emit(GeminiResult(rid, error=f"err.other:internal error: {e}", tag=tag))

    def _run_inner(self, rid, models, system, prompt, schema, temperature, image, tag) -> None:
        keys = find_api_keys(self.secrets_path)
        if not keys:
            self.finished.emit(GeminiResult(rid, error="err.no_key", tag=tag))
            return
        result = None
        for i, key in enumerate(keys):
            result = self._with_key(rid, key, models, system, prompt, schema, temperature, image, tag,
                                    last=i + 1 == len(keys))
            if result.error not in KEY_PROBLEMS:
                break
            if i + 1 < len(keys):
                log.info("Key #%d: %s; trying backup key #%d", i + 1, result.error, i + 2)
        self.finished.emit(result)

    def _with_key(self, rid, key, models, system, prompt, schema, temperature, image, tag,
                  last: bool = True) -> GeminiResult:
        """Try the models in order with one key; the result (or error) of the last try."""
        friendly = "err.no_model"
        now = time.monotonic()
        usable = [m for m in models if self._skip_until.get((key, m), 0) <= now]
        if not usable and not last:
            return GeminiResult(rid, error="err.quota", tag=tag)  # this key is resting: go straight to the backup
        for model in usable or list(models):
            for attempt in range(OVERLOAD_RETRIES + 1):
                try:
                    try:
                        text = self._call(key, model, system, prompt, schema, temperature, image)
                    except GeminiError as e:
                        if e.status != 400 or "API key" in str(e):
                            raise
                        # Some models reject responseSchema; retry in plain JSON mode.
                        log.info("%s rejected the schema (%s); retrying without it", model, e)
                        text = self._call(key, model, system, prompt, None, temperature, image)
                    return GeminiResult(rid, _extract_json(text), model, tag=tag)
                except GeminiError as e:
                    log.warning("Gemini %s attempt %d: %s", model, attempt + 1, e)
                    friendly = _friendly(e)
                    if e.status in OVERLOADED and attempt < OVERLOAD_RETRIES:
                        time.sleep(1.5 * (attempt + 1))  # usually a short spike
                        continue
                    if e.status in (403, 404) or (e.status == 429 and "quota" in str(e).lower()):
                        # no access / no quota on this key (e.g. Pro on the free tier): skip it for a while
                        self._skip_until[(key, model)] = time.monotonic() + (UNAVAILABLE_SKIP_S if e.status != 429 else 600)
                        self.unavailable.add(model)
                    if e.status in RETRY_NEXT_MODEL:
                        break  # try the next model
                    return GeminiResult(rid, error=friendly, tag=tag)  # bad key, blocked prompt...: other models won't help
                except TimeoutError:
                    log.warning("Gemini %s timed out", model)
                    friendly = "err.timeout"
                    break
                except (urllib.error.URLError, OSError) as e:
                    log.warning("Gemini network error: %s", getattr(e, "reason", e))
                    return GeminiResult(rid, error="err.offline", tag=tag)
                except (ValueError, KeyError) as e:
                    log.warning("Gemini %s unreadable reply: %s", model, e)
                    friendly = "err.unreadable"
                    break
        return GeminiResult(rid, error=friendly, tag=tag)

    @staticmethod
    def _call(key, model, system, prompt, schema, temperature, image=None) -> str:
        gen = {"temperature": temperature, "responseMimeType": "application/json"}
        if schema is not None:
            gen["responseSchema"] = schema
        parts = [{"text": prompt}]
        if image is not None:
            data, mime = image
            parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}})
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen,
        }
        req = urllib.request.Request(
            ENDPOINT.format(model=model), data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
            except (ValueError, OSError):
                msg = ""
            raise GeminiError(f"HTTP {e.code} {msg[:200]}".strip(), e.code) from None

        fb = payload.get("promptFeedback", {})
        if fb.get("blockReason"):
            raise GeminiError(f"blocked: {fb['blockReason']}")
        cands = payload.get("candidates") or []
        if not cands:
            raise GeminiError("empty response")
        parts = cands[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise GeminiError(f"no text (finishReason={cands[0].get('finishReason')})")
        return text
