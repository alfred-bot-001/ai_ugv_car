"""Live map, pose, scan, path, state and log frames for the console's map page.

Pure encoding plus the stream sender; the rclpy node lives in nav_relay_node. Frames go
to pi-agent's 0600 UNIX socket (nav/stream.sock), which forwards them to the Mini only
while someone has the map page open. Nothing here can move the robot.

Wire format, one frame per message: "!I" packet length, then the packet: "!I" metadata
length, UTF-8 JSON metadata with a "kind", and a payload (only maps carry one: a PNG).
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import socket
import struct
import threading
import time
import zlib
from array import array
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .loc_health import scan_endpoints
from .stall import Pose2D, wrap

STREAM_SOCKET = Path("/board/stream.sock")
MAX_PACKET = 2_000_000
MAX_SCAN_POINTS = 180
MAX_PATH_POINTS = 200
MAX_LOG_TEXT = 500
LOG_REPEAT_WINDOW_S = 30.0
OCCUPIED = 65  # same threshold as map_saver and loc_health

# Palette indexes in the map PNG; the page recolors them for its theme.
FREE, WALL, UNKNOWN = 0, 1, 2
PALETTE = ((245, 247, 244), (32, 52, 45), (205, 211, 208))

LEVELS = {10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR", 50: "FATAL"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def packet(kind: str, meta: dict[str, Any], payload: bytes = b"") -> bytes:
    body = json.dumps({"kind": kind, **meta}, allow_nan=False, separators=(",", ":")).encode()
    return struct.pack("!I", len(body)) + body + payload


def frame(data: bytes) -> bytes:
    if len(data) > MAX_PACKET:
        raise ValueError("packet too large")
    return struct.pack("!I", len(data)) + data


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", zlib.crc32(tag + data))


def _occupancy_table() -> bytes:
    """int8 occupancy (as unsigned bytes) -> palette index; -1 (255) and junk are unknown."""
    table = bytearray([UNKNOWN]) * 256
    for value in range(0, 101):
        table[value] = WALL if value >= OCCUPIED else FREE
    return bytes(table)


TABLE = _occupancy_table()


def grid_bytes(data: bytes | Sequence[int]) -> bytes:
    """OccupancyGrid.data as unsigned bytes (rclpy hands over an int8 array)."""
    if isinstance(data, bytes):
        return data
    if isinstance(data, array):
        return data.tobytes()
    return array("b", data).tobytes()


def encode_map(
    width: int,
    height: int,
    data: bytes | Sequence[int],
    resolution: float,
    origin_x: float,
    origin_y: float,
) -> tuple[dict[str, Any], bytes]:
    """Palette PNG with north up: the first PNG row is the grid's last (highest y) row."""
    if width <= 0 or height <= 0 or width * height > 4_000_000:
        raise ValueError("map size out of range")
    cells = grid_bytes(data)
    if len(cells) != width * height:
        raise ValueError("map data does not match its size")
    index = cells.translate(TABLE)
    rows = b"".join(
        b"\x00" + index[row * width : (row + 1) * width] for row in range(height - 1, -1, -1)
    )
    header = struct.pack("!IIBBBBB", width, height, 8, 3, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"PLTE", bytes(c for rgb in PALETTE for c in rgb))
        + _chunk(b"IDAT", zlib.compress(rows, 6))
        + _chunk(b"IEND", b"")
    )
    meta = {
        "width": width,
        "height": height,
        "resolution": round(resolution, 6),
        "origin": [round(origin_x, 4), round(origin_y, 4)],
        "free_cells": index.count(FREE),
    }
    return meta, png


def compose(a: Pose2D, b: Pose2D) -> Pose2D:
    """Pose b expressed in a's parent frame (2D transform chaining)."""
    c, s = math.cos(a.yaw), math.sin(a.yaw)
    return Pose2D(a.x + c * b.x - s * b.y, a.y + s * b.x + c * b.y, wrap(a.yaw + b.yaw))


def scan_points(
    ranges: Sequence[float],
    angle_min: float,
    angle_increment: float,
    sensor: Pose2D,
    range_min: float,
    range_max: float,
    limit: int = MAX_SCAN_POINTS,
) -> list[list[float]]:
    points = scan_endpoints(ranges, angle_min, angle_increment, sensor, range_min, range_max)
    step = max(1, math.ceil(len(points) / limit))
    return [[round(x, 2), round(y, 2)] for x, y in points[::step]]


def path_points(
    points: Sequence[tuple[float, float]], limit: int = MAX_PATH_POINTS
) -> list[list[float]]:
    if not points:
        return []
    step = max(1, math.ceil(len(points) / limit))
    kept = list(points[::step])
    if kept[-1] != points[-1]:
        kept.append(points[-1])
    return [[round(x, 2), round(y, 2)] for x, y in kept]


def guarded(
    callback: Callable[..., None],
    warn: Callable[[str], None],
    clock: Callable[[], float] = time.monotonic,
    every: float = 30.0,
) -> Callable[..., None]:
    """Run a ROS callback, reporting failures instead of raising them.

    The launch file tears the whole stack down when any process exits; the map page
    feed must never take mapping or navigation with it.
    """
    last: dict[str, float] = {}

    def run(*args: Any) -> None:
        try:
            callback(*args)
        except Exception as error:  # noqa: BLE001 - a display feed fails soft
            key = f"{getattr(callback, '__name__', 'callback')}: {type(error).__name__}"
            now = clock()
            if now - last.get(key, -every) >= every:
                last[key] = now
                warn(f"{key}: {error}")

    return run


class LogFilter:
    """rosout lines worth showing: INFO and up, colors stripped, repeats folded."""

    def __init__(self, window: float = LOG_REPEAT_WINDOW_S) -> None:
        self.window = window
        self.seen: dict[tuple[str, str], tuple[float, int]] = {}

    def accept(self, now: float, level: int, node: str, text: str) -> dict[str, Any] | None:
        if level < 20:
            return None
        text = ANSI.sub("", text).strip()[:MAX_LOG_TEXT]
        if not text:
            return None
        key = (node[:80], text)
        previous = self.seen.get(key)
        repeats = 0
        if previous is not None:
            first, repeats = previous
            if now - first < self.window:
                self.seen[key] = (first, repeats + 1)
                return None
        self.seen[key] = (now, 0)
        if len(self.seen) > 2000:
            self.seen = {k: v for k, v in self.seen.items() if now - v[0] < self.window}
        name = LEVELS.get(level, "INFO" if level < 30 else "ERROR")
        return {"level": name, "node": key[0], "text": text, "repeat": repeats}


class StreamSender:
    """Background writer to pi-agent; keeps the newest frame per kind and recent logs.

    Pose, scan, plan and state are latest-wins; the map is resent after a reconnect;
    log packets queue up to a bound. A missing or slow pi-agent never blocks ROS.
    """

    LATEST = ("map", "plan", "state", "scan", "pose")

    def __init__(
        self,
        path: Path = STREAM_SOCKET,
        connect: Callable[[Path], socket.socket] | None = None,
        retry: float = 2.0,
    ) -> None:
        self.path = path
        self.connect = connect or self._connect
        self.retry = retry
        self.lock = threading.Condition()
        self.latest: dict[str, bytes] = {}
        self.dirty: set[str] = set()
        self.logs: deque[bytes] = deque(maxlen=200)
        self.closed = False
        self.connected = False
        self.sent = 0

    @staticmethod
    def _connect(path: Path) -> socket.socket:
        connection = socket.socket(socket.AF_UNIX)
        connection.settimeout(1.0)
        connection.connect(str(path))
        return connection

    def put(self, kind: str, data: bytes) -> None:
        with self.lock:
            if kind == "log":
                self.logs.append(data)
            else:
                self.latest[kind] = data
                self.dirty.add(kind)
            self.lock.notify()

    def _take(self) -> list[bytes]:
        out = [self.latest[k] for k in self.LATEST if k in self.dirty]
        self.dirty.clear()
        out.extend(self.logs)
        self.logs.clear()
        return out

    def run(self) -> None:
        connection: socket.socket | None = None
        while not self.closed:
            if connection is None:
                try:
                    connection = self.connect(self.path)
                except OSError:
                    time.sleep(self.retry)
                    continue
                with self.lock:
                    self.dirty.update(self.latest)  # resend everything to a new listener
                self.connected = True
            with self.lock:
                if not self.dirty and not self.logs and not self.closed:
                    self.lock.wait(0.5)
                batch = self._take()
            try:
                for data in batch:
                    connection.sendall(frame(data))
                    self.sent += 1
            except OSError:
                with contextlib.suppress(OSError):
                    connection.close()
                connection = None
                self.connected = False
                time.sleep(self.retry)
        if connection is not None:
            connection.close()

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.lock.notify()
