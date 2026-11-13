"""Local link to the StickAI extension for VS Code.

StickAI listens on 127.0.0.1 only (a random free port) and writes the port and a
fresh secret token to %LOCALAPPDATA%\\StickAI\\vscode.json. The extension reads that
file, connects and must say hello with the token first; anything else is dropped.

Wire format: one JSON object per line (UTF-8), both ways.
  extension -> app: hello, context, explain
  app -> extension: welcome, start, chunk, done, error, explain_now
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

log = logging.getLogger("stickai.vscode")

PROTOCOL = 1
MAX_LINE = 2_000_000   # bytes; a message bigger than this is dropped with its connection
HELLO_TIMEOUT_MS = 4000


def info_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "StickAI" / "vscode.json"


class Client:
    def __init__(self, sock: QTcpSocket, cid: int):
        self.sock = sock
        self.id = cid
        self.ok = False            # said hello with the right token
        self.buf = bytearray()
        self.context: dict = {}    # what's open / selected in that VS Code window
        self.focused_at = 0.0      # time.monotonic() when that window last had focus
        self.workspace = ""
        self.version = ""          # the extension's version ("" = older than 0.3.2, which didn't say)


class Bridge(QObject):
    clients_changed = Signal(int)       # number of connected VS Code windows
    request = Signal(object, dict)      # (Client, message) for explain & co.

    def __init__(self, parent=None):
        super().__init__(parent)
        self.token = secrets.token_urlsafe(24)
        self.clients: dict[int, Client] = {}
        self._next = 0
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._accept)
        self.welcome: dict = {}             # extra fields for the welcome message (name, color, labels)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        if self.server.isListening():
            return True
        if not self.server.listen(QHostAddress(QHostAddress.SpecialAddress.LocalHost), 0):
            log.warning("VS Code link: can't listen: %s", self.server.errorString())
            return False
        p = info_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"port": self.server.serverPort(), "token": self.token,
                                       "pid": os.getpid(), "protocol": PROTOCOL}), encoding="utf-8")
            tmp.replace(p)
        except OSError as e:
            log.warning("VS Code link: can't write %s: %s", p, e)
        log.info("VS Code link on 127.0.0.1:%d", self.server.serverPort())
        return True

    def stop(self) -> None:
        for c in list(self.clients.values()):
            c.sock.abort()
        self.clients.clear()
        if self.server.isListening():
            self.server.close()
            try:
                p = info_path()
                if json.loads(p.read_text(encoding="utf-8")).get("pid") == os.getpid():
                    p.unlink()
            except (OSError, ValueError):
                pass
        self.clients_changed.emit(0)

    # ------------------------------------------------------------ queries
    def connected(self) -> list[Client]:
        return [c for c in self.clients.values() if c.ok]

    def active(self) -> Client | None:
        """The VS Code window that had focus most recently."""
        cs = self.connected()
        return max(cs, key=lambda c: c.focused_at) if cs else None

    def recently_focused(self, seconds: float) -> Client | None:
        c = self.active()
        if c is not None and (c.context.get("focused") or time.monotonic() - c.focused_at < seconds):
            return c
        return None

    def send(self, client: Client | None, msg: dict) -> bool:
        if client is None or client.id not in self.clients or not client.ok:
            return False
        client.sock.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
        return True

    # ------------------------------------------------------------ sockets
    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            if not sock.peerAddress().isLoopback():
                sock.abort()
                continue
            self._next += 1
            c = Client(sock, self._next)
            self.clients[c.id] = c
            sock.readyRead.connect(lambda c=c: self._read(c))
            sock.disconnected.connect(lambda c=c: self._gone(c))
            QTimer.singleShot(HELLO_TIMEOUT_MS, lambda c=c: (not c.ok and c.id in self.clients) and c.sock.abort())

    def _gone(self, c: Client) -> None:
        if self.clients.pop(c.id, None) is not None:
            c.sock.deleteLater()
            if c.ok:
                log.info("VS Code window disconnected (%s)", c.workspace or "no folder")
                self.clients_changed.emit(len(self.connected()))

    def _read(self, c: Client) -> None:
        c.buf += bytes(c.sock.readAll().data())
        while True:
            i = c.buf.find(b"\n")
            if i < 0:
                if len(c.buf) > MAX_LINE:
                    log.warning("VS Code link: oversized message, dropping the connection")
                    c.sock.abort()
                return
            line, c.buf = bytes(c.buf[:i]), c.buf[i + 1:]
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(msg, dict):
                self._handle(c, msg)

    def _handle(self, c: Client, msg: dict) -> None:
        kind = msg.get("type")
        if not c.ok:
            if kind == "hello" and secrets.compare_digest(str(msg.get("token", "")), self.token):
                c.ok = True
                c.workspace = str(msg.get("workspace", ""))[:200]
                c.version = str(msg.get("version", ""))[:20]
                c.focused_at = time.monotonic() if msg.get("focused") else 0.0
                self.send(c, {"type": "welcome", "protocol": PROTOCOL, **self.welcome})
                log.info("VS Code window connected (%s)", c.workspace or "no folder")
                self.clients_changed.emit(len(self.connected()))
            else:
                c.sock.abort()
            return
        if kind == "context":
            c.context = msg
            if msg.get("focused"):
                c.focused_at = time.monotonic()
        else:
            self.request.emit(c, msg)

    def broadcast_welcome(self) -> None:
        for c in self.connected():
            self.send(c, {"type": "welcome", "protocol": PROTOCOL, **self.welcome})
