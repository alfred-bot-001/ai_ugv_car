"""Map page feed: PNG maps, transforms, scan and path thinning, log folding, the sender."""

import json
import math
import socket
import struct
import tempfile
import threading
import time
import zlib
from array import array
from pathlib import Path

import pytest

from ugv_nav_tools.nav_relay import (
    FREE,
    PALETTE,
    UNKNOWN,
    WALL,
    LogFilter,
    StreamSender,
    compose,
    encode_map,
    frame,
    grid_bytes,
    guarded,
    packet,
    path_points,
    scan_points,
)
from ugv_nav_tools.stall import Pose2D


def read_png(png: bytes) -> tuple[int, int, bytes, list[bytes]]:
    """Width, height, palette and the unfiltered rows (top first) of an 8-bit palette PNG."""
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    offset, chunks = 8, {}
    idat = b""
    while offset < len(png):
        (length,) = struct.unpack_from("!I", png, offset)
        tag = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack_from("!I", png, offset + 8 + length)
        assert crc == zlib.crc32(tag + data)
        if tag == b"IDAT":
            idat += data
        chunks[tag] = data
        offset += 12 + length
    width, height, depth, color = struct.unpack_from("!IIBB", chunks[b"IHDR"])
    assert (depth, color) == (8, 3) and b"IEND" in chunks
    raw = zlib.decompress(idat)
    rows = [raw[r * (width + 1) : (r + 1) * (width + 1)] for r in range(height)]
    assert all(row[0] == 0 for row in rows)
    return width, height, chunks[b"PLTE"], [row[1:] for row in rows]


def test_map_png_is_north_up_with_three_classes() -> None:
    # Grid rows go up in y: row 0 is the bottom. Values: unknown, free, occupancy grades.
    data = [-1, 0, 50, 64, 65, 100, 120, -1]  # 4 x 2
    meta, png = encode_map(4, 2, data, 0.05, -1.0, -2.0)
    width, height, palette, rows = read_png(png)
    assert (width, height) == (4, 2)
    assert palette == bytes(c for rgb in PALETTE for c in rgb)
    assert rows[1] == bytes([UNKNOWN, FREE, FREE, FREE])  # grid row 0 is the bottom PNG row
    assert rows[0] == bytes([WALL, WALL, UNKNOWN, UNKNOWN])
    assert meta == {
        "width": 4,
        "height": 2,
        "resolution": 0.05,
        "origin": [-1.0, -2.0],
        "free_cells": 3,
    }
    same, _ = encode_map(4, 2, array("b", data), 0.05, -1.0, -2.0)
    assert same == meta
    assert grid_bytes(bytes([255, 0])) == b"\xff\x00"
    with pytest.raises(ValueError, match="size"):
        encode_map(3, 2, data, 0.05, 0, 0)
    with pytest.raises(ValueError, match="range"):
        encode_map(0, 2, [], 0.05, 0, 0)


def test_transforms_scan_and_path_thinning() -> None:
    chained = compose(Pose2D(1.0, 2.0, math.pi / 2), Pose2D(0.5, 0.0, math.pi / 2))
    assert chained.x == pytest.approx(1.0) and chained.y == pytest.approx(2.5)
    assert chained.yaw == pytest.approx(math.pi)
    ranges = [1.0] * 450 + [float("inf"), 0.0]
    points = scan_points(ranges, 0.0, 2 * math.pi / 452, Pose2D(0, 0, 0), 0.05, 12.0)
    assert 150 <= len(points) <= 180
    assert points[0] == [1.0, 0.0]
    assert all(abs(math.hypot(x, y) - 1.0) < 0.02 for x, y in points)
    long_path = [(i * 0.01, 0.0) for i in range(1001)]
    thinned = path_points(long_path)
    assert len(thinned) <= 201 and thinned[0] == [0.0, 0.0] and thinned[-1] == [10.0, 0.0]
    assert path_points([]) == []


def test_log_filter_folds_repeats_and_strips_colors() -> None:
    logs = LogFilter(window=30)
    assert logs.accept(0, 10, "amcl", "debug noise") is None
    line = logs.accept(0, 20, "lifecycle_manager", "\x1b[34m\x1b[1mActivating amcl\x1b[0m\x1b[0m")
    assert line == {
        "level": "INFO",
        "node": "lifecycle_manager",
        "text": "Activating amcl",
        "repeat": 0,
    }
    spam = "KeepoutFilter: Filter mask was not received"
    assert logs.accept(1, 30, "global_costmap", spam)["level"] == "WARN"
    for t in (3, 5, 7):
        assert logs.accept(t, 30, "global_costmap", spam) is None
    assert logs.accept(32, 30, "global_costmap", spam)["repeat"] == 3
    assert logs.accept(40, 40, "bt_navigator", "   ") is None
    assert logs.accept(41, 50, "x", "y" * 900)["text"] == "y" * 500
    assert logs.accept(42, 50, "x", "boom")["level"] == "FATAL"


def test_packets_and_frames() -> None:
    data = packet("pose", {"x": 1.5}, b"")
    (size,) = struct.unpack_from("!I", data)
    assert json.loads(data[4 : 4 + size]) == {"kind": "pose", "x": 1.5}
    framed = frame(data)
    assert struct.unpack_from("!I", framed)[0] == len(data)
    with pytest.raises(ValueError):
        frame(b"x" * 2_000_001)


def read_frames(connection: socket.socket, count: int) -> list[dict]:
    buffer, out = b"", []
    connection.settimeout(2)
    while len(out) < count:
        while len(buffer) >= 4 and len(buffer) >= 4 + struct.unpack_from("!I", buffer)[0]:
            (size,) = struct.unpack_from("!I", buffer)
            data, buffer = buffer[4 : 4 + size], buffer[4 + size :]
            (meta_size,) = struct.unpack_from("!I", data)
            out.append(json.loads(data[4 : 4 + meta_size]))
        if len(out) >= count:
            break
        buffer += connection.recv(65536)
    return out


def test_sender_resends_the_map_after_a_reconnect() -> None:
    short = tempfile.TemporaryDirectory(dir="/tmp")  # AF_UNIX paths are short on macOS
    path = Path(short.name) / "stream.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(2)
    server.settimeout(2)
    sender = StreamSender(path, retry=0.05)
    sender.put("map", packet("map", {"seq": 1}, b"png"))
    sender.put("pose", packet("pose", {"x": 0}))
    sender.put("pose", packet("pose", {"x": 1}))  # latest wins
    for i in range(3):
        sender.put("log", packet("log", {"text": str(i)}))
    thread = threading.Thread(target=sender.run, daemon=True)
    thread.start()
    first, _ = server.accept()
    got = read_frames(first, 5)
    assert [m["kind"] for m in got] == ["map", "pose", "log", "log", "log"]
    assert got[1]["x"] == 1
    first.close()
    sender.put("state", packet("state", {"mode": "mapping"}))  # fails, then reconnects
    second, _ = server.accept()
    kinds = [m["kind"] for m in read_frames(second, 3)]
    assert kinds[:2] == ["map", "state"] or "map" in kinds
    sender.close()
    thread.join(timeout=2)
    assert not thread.is_alive()
    second.close()
    server.close()
    short.cleanup()


def test_sender_waits_for_a_missing_listener(tmp_path: Path) -> None:
    sender = StreamSender(tmp_path / "absent.sock", retry=0.01)
    thread = threading.Thread(target=sender.run, daemon=True)
    thread.start()
    time.sleep(0.05)
    assert not sender.connected
    sender.close()
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_guarded_callbacks_report_instead_of_raising() -> None:
    warnings: list[str] = []
    now = [0.0]
    calls: list[int] = []

    def send_map(value: int) -> None:
        calls.append(value)
        if value < 0:
            raise ValueError("map size out of range")

    safe = guarded(send_map, warnings.append, clock=lambda: now[0], every=30)
    safe(1)
    safe(-1)
    safe(-2)  # same failure within 30 s: not repeated
    now[0] = 31
    safe(-3)
    assert calls == [1, -1, -2, -3]
    assert warnings == [
        "send_map: ValueError: map size out of range",
        "send_map: ValueError: map size out of range",
    ]
