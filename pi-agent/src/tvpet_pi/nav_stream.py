"""Receives the ugv-nav relay's map page feed on a 0600 UNIX socket (read-only data).

The container connects to nav/stream.sock and writes "!I"-length-prefixed packets
(see navframe). Invalid packets are counted and dropped; nothing here touches the UART.
"""

import contextlib
import os
import socket
import struct
import threading
import time
from pathlib import Path

from .navframe import MAX_NAV_PACKET, NavCache, make_packet

READ_TIMEOUT_S = 5.0  # the relay sends state every second


class NavStream:
    def __init__(self, root: Path, closed: threading.Event) -> None:
        self.root = root
        self.closed = closed
        self.cache = NavCache()
        self.connected = False
        self.received = 0
        self.rejected = 0

    def log(self, level: str, text: str) -> None:
        """A pi-agent event for the page's log, e.g. a safety stop."""
        meta = {"stamp": round(time.time(), 3), "level": level, "node": "pi-agent"}
        self.cache.add(make_packet("log", {**meta, "text": text[:500], "repeat": 0}))

    def read(self, connection: socket.socket) -> None:
        """Consume frames until the relay disconnects, goes quiet or breaks framing."""
        buffer = b""
        while not self.closed.is_set():
            while len(buffer) >= 4:
                (size,) = struct.unpack_from("!I", buffer)
                if not 4 <= size <= MAX_NAV_PACKET:
                    return
                if len(buffer) < 4 + size:
                    break
                data, buffer = buffer[4 : 4 + size], buffer[4 + size :]
                try:
                    self.cache.add(data)
                    self.received += 1
                except ValueError:
                    self.rejected += 1
            part = connection.recv(262144)
            if not part:
                return
            buffer += part

    def serve(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / "stream.sock"
        path.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(1)
            server.settimeout(0.3)
            while not self.closed.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(READ_TIMEOUT_S)
                    self.connected = True
                    with contextlib.suppress(OSError):
                        self.read(connection)
                    self.connected = False
        path.unlink(missing_ok=True)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve, daemon=True, name="nav-stream")
        thread.start()
        return thread
