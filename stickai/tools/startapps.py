"""The apps in your Start menu, so "open Spotify" / "open Calculator" just works.

Windows lists them (desktop programs AND Store apps) with Get-StartApps: a
display name and an AppID. Any of them starts with
    explorer.exe shell:AppsFolder\\<AppID>
exactly like clicking it in the Start menu. The list is read once in the
background at start-up (about 2 s) and refreshed every 30 minutes; nothing is
sent anywhere, the model only names the app it wants and it's matched here.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time

log = logging.getLogger("stickai.startapps")
REFRESH_S = 1800
CREATE_NO_WINDOW = 0x08000000
# never started for you, whatever the name match says
SKIP = re.compile(r"uninstall|удал|deinštal|odinštal|odstrániť|setup|installer|установ|readme|help|справк|"
                  r"license|licen[cz]|support center", re.IGNORECASE)


def _words(s: str) -> list[str]:
    return re.findall(r"[^\W_]+", s.lower())


class StartApps:
    def __init__(self):
        self.items: list[tuple[str, str]] = []   # (name, AppID)
        self._loaded = 0.0
        self._lock = threading.Lock()
        self.refresh()

    def refresh(self) -> None:
        threading.Thread(target=self._load, name="startapps", daemon=True).start()

    def _load(self) -> None:
        cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
               "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                                 capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            data = json.loads(out.stdout.decode("utf-8", "replace") or "[]")
            if isinstance(data, dict):
                data = [data]
            items = [(str(d.get("Name", "")).strip(), str(d.get("AppID", "")).strip()) for d in data]
            items = [(n, a) for n, a in items if n and a and not SKIP.search(n)]
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            log.info("Start-menu apps unavailable: %s", e)
            return
        with self._lock:
            self.items, self._loaded = items, time.monotonic()
        log.info("Start menu: %d apps", len(items))

    def find(self, name: str) -> tuple[str, str] | None:
        """Best (name, AppID) for what the model / user called it, or None.
        Matches the display name, then words of the AppID ("Calculator" finds
        "Калькулятор" through Microsoft.WindowsCalculator_...)."""
        if time.monotonic() - self._loaded > REFRESH_S:
            self.refresh()
        want = " ".join(_words(name))
        if not want:
            return None
        with self._lock:
            items = list(self.items)
        best, best_rank = None, 99
        for n, appid in items:
            nl = " ".join(_words(n))
            ids = " ".join(_words(re.sub(r"([a-z])([A-Z])", r"\1 \2", appid.rsplit("\\", 1)[-1])))
            if nl == want:
                rank = 0
            elif nl.startswith(want) or want.startswith(nl):
                rank = 1
            elif re.search(rf"\b{re.escape(want)}\b", nl):
                rank = 2
            elif re.search(rf"\b{re.escape(want)}", ids):
                rank = 3
            else:
                continue
            if appid.startswith(("http://", "https://")):
                rank += 3  # a web link in the Start menu: only if nothing better
            if rank < best_rank:
                best, best_rank = (n, appid), rank
        return best

    @staticmethod
    def launch(appid: str) -> None:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{appid}"], creationflags=CREATE_NO_WINDOW)
