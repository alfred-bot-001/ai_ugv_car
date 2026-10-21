"""Bridge pieces that decide whether the chassis may move, tested without ROS."""

import json
import math
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from ugv_nav_tools.board_client import BoardClient, BoardError
from ugv_nav_tools.motion_gate import (
    FRONT_STOP_M,
    Perception,
    front_ranges,
    gate,
    rotation_space_clear,
)
from ugv_nav_tools.wheel_odometry import TRACK_WIDTH_M, WheelOdometry

NOW = 100.0
FRESH = Perception(scan_at=NOW, front_count=50, front_min=1.0, laser_odom_at=NOW)


def test_gate_passes_zero_without_perception_and_fresh_commands() -> None:
    blind = Perception(0.0, 0, 0.0, 0.0)
    assert gate(0, 0, NOW, blind, lambda: False).stop is False
    assert gate(0.05, 0.1, NOW, FRESH, lambda: False) == gate(0.05, 0.1, NOW, FRESH, lambda: True)
    decision = gate(0.05, 0.1, NOW, FRESH, lambda: False)
    assert (decision.linear, decision.angular, decision.stop) == (0.05, 0.1, False)


@pytest.mark.parametrize(
    ("seen", "reason"),
    [
        (Perception(NOW, 50, 1.0, NOW - 1), "laser odometry"),
        (Perception(NOW - 1, 50, 1.0, NOW), "scan"),
        (Perception(NOW, 3, 1.0, NOW), "scan"),
    ],
)
def test_gate_stops_on_stale_or_thin_perception(seen: Perception, reason: str) -> None:
    decision = gate(0.05, 0, NOW, seen, lambda: True)
    assert decision.stop and reason in decision.reason


def test_gate_near_wall_allows_only_verified_slow_turns() -> None:
    near = Perception(NOW, 50, FRONT_STOP_M - 0.01, NOW)
    turn = gate(0.04, 0.2, NOW, near, lambda: True)
    assert (turn.linear, turn.angular, turn.stop) == (0.0, 0.15, False)
    assert gate(0.04, 0.2, NOW, near, lambda: False).stop
    assert gate(0.04, 0.0, NOW, near, lambda: True).stop


def test_front_ranges_use_the_mount_yaw() -> None:
    # Scan bins at -90, 0, 90 and 180 deg; only -90 deg is the robot's front after +90 mount.
    found = front_ranges([1.0, 2.0, 3.0, math.nan], -math.pi / 2, math.pi / 2)
    assert found == [1.0]
    assert front_ranges([math.nan], -math.pi / 2, 0.1) == []


def test_rotation_space_requires_known_free_cells() -> None:
    free = [0] * 100
    assert rotation_space_clear(free, 10, 10, 0.1, 0, 0, 0.5, 0.5, radius=0.3)
    assert rotation_space_clear(free, 10, 10, 0.1, 0, 0, 0.5, 0.5)  # default turn radius
    occupied = free.copy()
    occupied[5 * 10 + 6] = 100
    assert not rotation_space_clear(occupied, 10, 10, 0.1, 0, 0, 0.5, 0.5, radius=0.3)
    unknown = free.copy()
    unknown[4 * 10 + 4] = -1
    assert not rotation_space_clear(unknown, 10, 10, 0.1, 0, 0, 0.5, 0.5, radius=0.3)
    assert not rotation_space_clear(free, 10, 10, 0.1, 0, 0, 0.1, 0.5, radius=0.3)  # off map
    assert not rotation_space_clear(free, 10, 9, 0.1, 0, 0, 0.5, 0.5)
    assert not rotation_space_clear(free, 10, 10, 0.0, 0, 0, 0.5, 0.5)


def test_wheel_odometry_integrates_and_rejects_jumps() -> None:
    odom = WheelOdometry()
    odom.update(0.0, 0.0, 0.0, 0.0)
    pose = odom.update(1.0, 1.0, 0.1, 0.1)
    assert (pose.x, pose.y, pose.yaw, pose.linear) == pytest.approx((0.0, 0.0, 0.0, 0.1))
    assert odom.rejected == 1  # 1 m in one sample is a counter jump
    pose = odom.update(1.1, 1.1, 0.1, 0.1)
    assert pose.x == pytest.approx(0.1)
    quarter = math.pi / 2 * TRACK_WIDTH_M / 2
    pose = odom.update(1.1 - quarter, 1.1 + quarter, -0.05, 0.05)
    assert pose.yaw == pytest.approx(math.pi / 2)
    assert pose.angular == pytest.approx(0.1 / TRACK_WIDTH_M)
    with pytest.raises(ValueError):
        odom.update(math.nan, 0, 0, 0)


@pytest.fixture
def server() -> Iterator[tuple[Path, list[bytes]]]:
    replies: list[bytes] = []
    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        path = Path(short) / "board.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        listener.listen(4)
        received: list[bytes] = []

        def serve() -> None:
            while replies:
                connection, _ = listener.accept()
                with connection:
                    received.append(connection.recv(4096))
                    connection.sendall(replies.pop(0))

        replies += [
            b'{"ok":true,"telemetry":{"T":1001}}\n',
            b'{"ok":false,"error":"motion is disarmed"}\n',
            b"garbage\n",
            b'{"ok":true}\n',
        ]
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        yield path, received
        thread.join(timeout=2)
        listener.close()


def test_board_client_round_trip_and_errors(server: tuple[Path, list[bytes]]) -> None:
    path, received = server
    client = BoardClient(path, timeout=1)
    assert client.status()["telemetry"]["T"] == 1001
    with pytest.raises(BoardError, match="disarmed"):
        client.command(0.05, 0.0)
    with pytest.raises(BoardError, match="malformed"):
        client.stop()
    client.stop()
    assert [json.loads(r)["op"] for r in received] == ["status", "cmd", "stop", "stop"]
    with pytest.raises(BoardError, match="socket"):
        BoardClient(path.parent / "missing.sock").status()


def test_board_client_stop_reason_and_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []
    replies = iter(
        [
            {"ok": True},
            {"ok": True, "fault": {"reason": "stall_body", "at": 1.0}},
            {"ok": True, "fault": None},
        ]
    )
    client = BoardClient(Path("/nonexistent.sock"))
    monkeypatch.setattr(client, "request", lambda payload: sent.append(payload) or next(replies))
    client.stop(reason="stall_body")
    assert sent[-1] == {"op": "stop", "reason": "stall_body"}
    assert client.fault() == {"reason": "stall_body", "at": 1.0}
    assert client.fault() is None
