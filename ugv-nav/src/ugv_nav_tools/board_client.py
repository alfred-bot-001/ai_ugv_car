"""Client for pi-agent's local navigation socket; never opens a serial device."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

MAX_REPLY = 8192


class BoardError(RuntimeError):
    """pi-agent refused the request or the socket failed."""


class BoardClient:
    """One request per connection, newline-delimited JSON, matching tvpet_pi.nav."""

    def __init__(self, path: Path, timeout: float = 0.15) -> None:
        self.path = path
        self.timeout = timeout

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(self.timeout)
                connection.connect(str(self.path))
                connection.sendall(json.dumps(payload, allow_nan=False).encode() + b"\n")
                data = b""
                while b"\n" not in data and len(data) < MAX_REPLY:
                    part = connection.recv(MAX_REPLY - len(data))
                    if not part:
                        break
                    data += part
        except OSError as error:
            raise BoardError(f"board socket: {error}") from error
        try:
            reply = json.loads(data)
        except ValueError as error:
            raise BoardError("malformed board reply") from error
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            error_text = reply.get("error") if isinstance(reply, dict) else None
            raise BoardError(str(error_text or "board refused request"))
        return reply

    def status(self) -> dict[str, Any]:
        return self.request({"op": "status"})

    def stop(self, reason: str | None = None) -> None:
        """A reason (stall_body, localization_lost) also ends pi-agent's arm session."""
        payload: dict[str, Any] = {"op": "stop"}
        if reason:
            payload["reason"] = reason
        self.request(payload)

    def fault(self) -> dict[str, Any] | None:
        """pi-agent's latched safety stop for the current arm session, if any."""
        value = self.status().get("fault")
        return value if isinstance(value, dict) else None

    def command(self, linear: float, angular: float) -> None:
        self.request({"op": "cmd", "linear": linear, "angular": angular})
