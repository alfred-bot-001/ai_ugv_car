"""Map page packets shared by pi-agent and the Mini: validated, bounded and cached.

A packet is "!I" metadata length, UTF-8 JSON metadata with a "kind", then a payload that
only maps carry (a palette PNG). Produced by ugv_nav_tools.nav_relay in the ugv-nav
container; pi-agent adds "log" lines of its own and "agent" status. Read-only data.
"""

import json
import struct
import threading
from collections import deque
from typing import Any

NAV_KINDS = frozenset({"map", "pose", "scan", "plan", "state", "log", "agent"})
LATEST_KINDS = ("map", "plan", "state", "agent", "pose", "scan")
MAX_NAV_META = 65536
MAX_NAV_MAP = 1_048_576
MAX_NAV_PACKET = 4 + MAX_NAV_META + MAX_NAV_MAP
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite number {value}")


def parse_packet(data: bytes) -> tuple[str, dict[str, Any]]:
    """Kind and metadata of a valid packet; ValueError otherwise."""
    if len(data) < 4 or len(data) > MAX_NAV_PACKET:
        raise ValueError("invalid map packet size")
    (size,) = struct.unpack_from("!I", data)
    if not 2 <= size <= MAX_NAV_META or 4 + size > len(data):
        raise ValueError("invalid map packet metadata")
    meta = json.loads(data[4 : 4 + size], parse_constant=_reject_constant)
    if not isinstance(meta, dict) or meta.get("kind") not in NAV_KINDS:
        raise ValueError("unknown map packet kind")
    payload = data[4 + size :]
    if meta["kind"] == "map":
        if not payload.startswith(PNG_SIGNATURE) or len(payload) > MAX_NAV_MAP:
            raise ValueError("invalid map image")
    elif payload:
        raise ValueError("unexpected map packet payload")
    return str(meta["kind"]), meta


def make_packet(kind: str, meta: dict[str, Any]) -> bytes:
    body = json.dumps({**meta, "kind": kind}, allow_nan=False, separators=(",", ":")).encode()
    return struct.pack("!I", len(body)) + body


class NavCache:
    """Newest packet per kind, recent logs and a numbered backlog for streaming readers."""

    def __init__(self, logs: int = 200, backlog: int = 256) -> None:
        self.lock = threading.Lock()
        self.latest: dict[str, bytes] = {}
        self.logs: deque[bytes] = deque(maxlen=logs)
        self.backlog: deque[tuple[int, bytes]] = deque(maxlen=backlog)
        self.seq = 0

    def add(self, data: bytes) -> str:
        kind, _ = parse_packet(data)
        with self.lock:
            self.seq += 1
            if kind == "log":
                self.logs.append(data)
            else:
                self.latest[kind] = data
            self.backlog.append((self.seq, data))
        return kind

    def snapshot(self, logs: int = 100) -> tuple[int, list[bytes]]:
        """Everything a new reader needs: newest of each kind, then recent logs."""
        with self.lock:
            packets = [self.latest[k] for k in LATEST_KINDS if k in self.latest]
            return self.seq, packets + list(self.logs)[-logs:]

    def since(self, seq: int) -> tuple[int, list[bytes] | None]:
        """Packets after seq, or None when the reader fell behind the backlog."""
        with self.lock:
            if seq == self.seq:
                return seq, []
            if seq > self.seq or not self.backlog or self.backlog[0][0] > seq + 1:
                return self.seq, None
            return self.seq, [data for number, data in self.backlog if number > seq]

    def clear(self) -> None:
        with self.lock:
            self.latest.clear()
            self.logs.clear()
            self.backlog.clear()
