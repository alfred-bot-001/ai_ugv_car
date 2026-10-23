"""The ROS bridge may only move the chassis inside every existing safety gate."""

import json
import os
import socket
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from tvpet_pi.hardware import RUNAWAY_SPEED, Board
from tvpet_pi.nav import (
    PWM_LIMIT,
    REQUEST,
    WHEEL_FLOOR,
    NavBridge,
    StallMonitor,
    floor_wheels,
    read_arm,
    stick_from_wheels,
    wheel_targets,
)
from tvpet_pi.protocol import Stick
from tvpet_pi.safety import Guard, differential

TELEMETRY = b'{"T":1001,"L":0,"R":0,"v":12.1,"pan":0,"tilt":0}\n'


def write_arm(root: Path, **changes: Any) -> None:
    value = {
        "enabled": True,
        "id": "session-1",
        "expires_at": time.time() + 60,
        "units_verified": True,
        "calibrated": True,
    } | changes
    path = root / "arm.json"
    path.write_text(json.dumps(value))
    os.chmod(path, 0o600)


def test_read_arm_requires_every_operator_condition(tmp_path: Path) -> None:
    write_arm(tmp_path)
    arm = read_arm(tmp_path / "arm.json", time.time())
    assert arm.session_id == "session-1" and not arm.calibration and 0 < arm.remaining <= 60
    now = time.time()
    rejected: list[dict[str, Any]] = [
        {"enabled": False},
        {"expires_at": now - 1},
        {"expires_at": now + 121},
        {"expires_at": "soon"},
        {"units_verified": False},
        {"calibrated": False},
        {"calibrated": False, "mode": "calibration", "expires_at": now + 30},
        {"id": ""},
    ]
    for changes in rejected:
        write_arm(tmp_path, **changes)
        with pytest.raises(ValueError):
            read_arm(tmp_path / "arm.json", now)
    write_arm(tmp_path, calibrated=False, mode="calibration", expires_at=now + 4)
    assert read_arm(tmp_path / "arm.json", now).calibration
    write_arm(tmp_path)
    os.chmod(tmp_path / "arm.json", 0o644)
    with pytest.raises(ValueError, match="0600"):
        read_arm(tmp_path / "arm.json", now)


def test_requests_are_bounded_and_strict() -> None:
    assert REQUEST.validate_python({"op": "cmd", "linear": 0.05, "angular": -0.2}).linear == 0.05
    for bad in (
        {"op": "cmd", "linear": -0.01, "angular": 0},
        {"op": "cmd", "linear": 0.13, "angular": 0},
        {"op": "cmd", "linear": 0, "angular": 0.61},
        {"op": "cmd", "linear": float("nan"), "angular": 0},
        {"op": "cmd", "linear": 0, "angular": 0, "extra": 1},
        {"op": "drive"},
    ):
        with pytest.raises(ValidationError):
            REQUEST.validate_python(bad)


def test_wheel_mapping_round_trips_through_board_differential() -> None:
    left, right = wheel_targets(0.1, 0.2)
    assert right - left == pytest.approx(0.2 * 0.172)  # the firmware's T13 formula
    assert (left + right) / 2 == pytest.approx(0.1)
    for wheels in ((0.18, 0.18), (0.18, 0.0), (0.0, -0.18), (0.03, 0.05), (0.18, -0.18)):
        x, y = stick_from_wheels(*wheels)
        assert max(abs(x), abs(y)) <= 1
        assert differential(x, y, PWM_LIMIT) == pytest.approx(wheels)


@pytest.mark.parametrize(
    "linear, angular, expected",
    [
        (0.0, 0.0, (0.0, 0.0)),
        (0.10, 0.0, (0.10, 0.10)),
        (0.0, 0.5, (-WHEEL_FLOOR, WHEEL_FLOOR)),  # +-0.043 lifted: no stop-and-go spin
        (0.0, -0.2, (WHEEL_FLOOR, -WHEEL_FLOOR)),
        (0.03, 0.0, (WHEEL_FLOOR, WHEEL_FLOOR)),  # a crawl is lifted to the floor
        (0.06, 0.3, (0.0, 0.0858)),  # inner 0.034 m/s pivots instead of stuttering
        (0.12, 0.6, (WHEEL_FLOOR, 0.1716)),  # inner 0.068 m/s lifted to the floor
        (0.12, -0.6, (0.1716, WHEEL_FLOOR)),
    ],
)
def test_wheel_targets_stay_out_of_the_firmware_stutter_band(
    linear: float, angular: float, expected: tuple[float, float]
) -> None:
    left, right = wheel_targets(linear, angular)
    assert (left, right) == pytest.approx(expected)
    for side in (left, right):
        assert side == 0 or WHEEL_FLOOR <= abs(side) <= PWM_LIMIT


def test_floor_keeps_the_curvature_when_lifting_a_slow_arc() -> None:
    left, right = floor_wheels(0.02, 0.04)
    assert right == pytest.approx(WHEEL_FLOOR) and left == pytest.approx(WHEEL_FLOOR)
    left, right = floor_wheels(0.01, 0.04)  # inner falls below half the floor once lifted
    assert left == 0.0 and right == pytest.approx(WHEEL_FLOOR)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[NavBridge, Mock]]:
    uart = Mock()
    uart.in_waiting = 0
    uart.read.return_value = TELEMETRY
    monkeypatch.setattr("tvpet_pi.hardware.serial.Serial", Mock(return_value=uart))
    original = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, *a, **k: (
            "boot" if str(self) == "/proc/sys/kernel/random/boot_id" else original(self, *a, **k)
        ),
    )
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": 1,
                "port": "test-uart",
                "boot_id": "boot",
                "expires_at": time.time() + 600,
                "wheels_verified": True,
                "gimbal_verified": True,
                "unit_profile": "t1_scalar_limited",
                "max_command": 0.1,
                "firmware_timeout_ms": 300,
                "measured_stop_seconds": 0.4,
            }
        )
    )
    os.chmod(evidence, 0o600)
    board = Board("test-uart", Guard(), evidence)
    board.open()
    deadline = time.monotonic() + 2
    while "v" not in board.telemetry and time.monotonic() < deadline:
        time.sleep(0.01)  # the reader thread parses the first frame; slow under load
    nav_root = tmp_path / "nav"
    nav_root.mkdir()
    try:
        yield NavBridge(board, nav_root), uart
    finally:
        board.close()


def wheel_writes(uart: Mock) -> list[dict[str, Any]]:
    commands = [json.loads(call.args[0]) for call in uart.write.call_args_list]
    return [c for c in commands if c.get("T") == 1]


def test_status_is_read_only_and_requests_fast_telemetry(rig: tuple[NavBridge, Mock]) -> None:
    nav, _ = rig
    status = nav.dispatch({"op": "status"})
    assert status["ok"] and status["telemetry"]["v"] == 12.1 and not status["nav_owned"]
    assert nav.board.observe_until > time.monotonic()
    assert not nav.board.guard.lease


def test_motion_needs_arm_then_obeys_lease_and_watchdog(rig: tuple[NavBridge, Mock]) -> None:
    nav, uart = rig
    response = json.loads(nav.handle(b'{"op":"cmd","linear":0.05,"angular":0}\n'))
    assert not response["ok"] and not nav.board.guard.lease

    write_arm(nav.root)
    nav.board.guard.heartbeat()
    assert nav.dispatch({"op": "cmd", "linear": 0.05, "angular": 0}) == {"ok": True}
    assert nav.owned and nav.board.guard.press_deadline == nav.session_deadline
    left = wheel_writes(uart)[-1]["L"]
    assert 0 < left <= PWM_LIMIT

    nav.watchdog_tick()
    assert nav.owned  # a fresh command is within the 250 ms window
    nav.last_command_at -= 1
    uart.write.reset_mock()
    nav.watchdog_tick()
    assert not nav.owned and not nav.board.guard.lease
    assert wheel_writes(uart)[-1] == {"T": 1, "L": 0.0, "R": 0.0}


def test_manual_owner_estop_and_revocation_need_a_new_arm(rig: tuple[NavBridge, Mock]) -> None:
    nav, _ = rig
    write_arm(nav.root)
    guard = nav.board.guard
    guard.heartbeat()
    manual = nav.board.acquire()
    with pytest.raises(ValueError):
        nav.dispatch({"op": "cmd", "linear": 0.05, "angular": 0})
    assert guard.lease == manual
    nav.board.stop()

    nav.lease = ""
    assert nav.dispatch({"op": "cmd", "linear": 0.05, "angular": 0})["ok"]
    nav.board.stop()  # e.g. the web page stop button
    with pytest.raises(ValueError, match="re-arm"):
        nav.dispatch({"op": "cmd", "linear": 0.05, "angular": 0})
    write_arm(nav.root, id="session-2")
    assert nav.dispatch({"op": "cmd", "linear": 0.05, "angular": 0.1})["ok"]

    assert nav.dispatch({"op": "cmd", "linear": 0, "angular": 0}) == {"ok": True, "stopped": True}
    assert not guard.lease
    nav.board.stop(emergency=True)
    with pytest.raises(ValueError, match="estop"):
        nav.dispatch({"op": "cmd", "linear": 0.05, "angular": 0})


def test_calibration_arm_allows_only_short_straight_runs(rig: tuple[NavBridge, Mock]) -> None:
    nav, _ = rig
    write_arm(nav.root, calibrated=False, mode="calibration", expires_at=time.time() + 4)
    nav.board.guard.heartbeat()
    for request in (
        {"op": "cmd", "linear": 0.05, "angular": 0},
        {"op": "cmd", "linear": 0.07, "angular": 0, "source": "calibration"},
        {"op": "cmd", "linear": 0.05, "angular": 0.1, "source": "calibration"},
    ):
        with pytest.raises(ValueError, match="calibration"):
            nav.dispatch(request)
    request = {"op": "cmd", "linear": 0.05, "angular": 0, "source": "calibration"}
    assert nav.dispatch(request)["ok"]


def test_stale_telemetry_rejects_motion(rig: tuple[NavBridge, Mock]) -> None:
    nav, uart = rig
    write_arm(nav.root)
    uart.read.return_value = b""
    nav.board.received_at = time.monotonic() - 1
    with pytest.raises(ValueError):
        nav.dispatch({"op": "cmd", "linear": 0.05, "angular": 0})


def test_socket_round_trip_and_permissions(rig: tuple[NavBridge, Mock]) -> None:
    nav, _ = rig
    # macOS limits UNIX socket paths to 104 bytes, so avoid the long pytest tmp_path.
    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        nav.root = Path(short) / "nav"
        nav.start()
        path = nav.root / "board.sock"
        deadline = time.monotonic() + 2
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert path.stat().st_mode & 0o777 == 0o600
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))
            client.sendall(b'{"op":"status"}\n')
            reply = json.loads(client.makefile().readline())
        assert reply["ok"] and reply["telemetry"]["T"] == 1001
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))
            client.sendall(b"not json\n")
            assert not json.loads(client.makefile().readline())["ok"]
        nav.board.close()
        for thread in nav.threads:
            thread.join(timeout=2)
        assert not path.exists()


def test_joystick_stays_at_010_while_nav_may_reach_018(rig: tuple[NavBridge, Mock]) -> None:
    nav, uart = rig
    guard = nav.board.guard
    guard.heartbeat()
    lease = nav.board.acquire()
    stick = Stick(target="wheels", x=0, y=1, lease=lease, sequence=1)
    nav.board.move(stick)
    assert wheel_writes(uart)[-1] == {"T": 1, "L": 0.1, "R": 0.1}
    stick = Stick(target="wheels", x=0, y=1, lease=lease, sequence=2)
    nav.board.move(stick, maximum=PWM_LIMIT)
    assert wheel_writes(uart)[-1] == {"T": 1, "L": PWM_LIMIT, "R": PWM_LIMIT}
    nav.board.stop()


def test_joystick_speed_field_raises_the_wheel_ceiling_up_to_018(
    rig: tuple[NavBridge, Mock],
) -> None:
    nav, uart = rig
    guard = nav.board.guard
    guard.heartbeat()
    lease = nav.board.acquire()
    common = {"target": "wheels", "lease": lease}
    nav.board.move(Stick(x=0, y=1, sequence=1, speed=0.18, **common))
    assert wheel_writes(uart)[-1] == {"T": 1, "L": 0.18, "R": 0.18}
    nav.board.move(Stick(x=0, y=-1, sequence=2, speed=0.07, **common))
    assert wheel_writes(uart)[-1] == {"T": 1, "L": -0.07, "R": -0.07}
    # The explicit nav ceiling still wins over the stick field.
    nav.board.move(
        Stick(x=0, y=1, sequence=3, speed=0.07, **common),
        maximum=PWM_LIMIT,
    )
    assert wheel_writes(uart)[-1] == {"T": 1, "L": PWM_LIMIT, "R": PWM_LIMIT}
    nav.board.stop()


def test_runaway_threshold_sits_above_nav_ceiling(rig: tuple[NavBridge, Mock]) -> None:
    board = rig[0].board
    assert PWM_LIMIT < RUNAWAY_SPEED
    board.telemetry = {"L": 0.25, "R": 0.25, "v": 12.0, "pan": 0, "tilt": 0}
    board.received_at = time.monotonic()
    assert board.health_error() == ""
    board.telemetry["L"] = 0.31
    assert "速度" in board.health_error()


def test_board_health_allows_two_degrees_of_servo_overshoot(rig: tuple[NavBridge, Mock]) -> None:
    board = rig[0].board
    board.telemetry = {"L": 0, "R": 0, "v": 12.0, "pan": 171.9, "tilt": -31.9}
    board.received_at = time.monotonic()
    assert board.health_error() == ""
    board.telemetry["pan"] = 172.1
    assert "云台" in board.health_error()
    board.telemetry.update(pan=0, tilt=92.1)
    assert "云台" in board.health_error()


def test_stall_monitor_needs_a_commanded_wheel_still_for_the_window() -> None:
    monitor = StallMonitor(seconds=1.5)
    assert monitor.update(0.0, (0.07, 0.10), (0.07, 0.0)) is None
    assert monitor.update(1.4, (0.07, 0.10), (0.07, 0.0)) is None
    assert monitor.update(1.5, (0.07, 0.10), (0.07, 0.0)) == "right"

    # One encoder step (the firmware reports 0.064 m/s steps) restarts the window.
    monitor.reset()
    assert monitor.update(0.0, (0.1, 0.1), (0.0, 0.0)) is None
    assert monitor.update(1.0, (0.1, 0.1), (0.064, 0.064)) is None
    assert monitor.update(2.0, (0.1, 0.1), (0.0, 0.0)) is None
    assert monitor.update(3.4, (0.1, 0.1), (0.0, 0.0)) is None

    # A still (zero) target never trips; a reversed wheel is judged by magnitude.
    monitor.reset()
    for now in (10.0, 15.0, 20.0):
        assert monitor.update(now, (0.0, 0.0), (0.0, 0.0)) is None
    monitor.reset()
    assert monitor.update(40.0, (-0.08, 0.08), (0.0, 0.08)) is None
    assert monitor.update(41.6, (-0.08, 0.08), (0.0, 0.08)) == "left"


def test_wheel_stall_stops_and_needs_a_new_arm(rig: tuple[NavBridge, Mock]) -> None:
    nav, uart = rig
    write_arm(nav.root)
    nav.board.guard.heartbeat()
    assert nav.dispatch({"op": "cmd", "linear": 0.1, "angular": 0})["ok"]
    nav.stall_monitor.seconds = 0.0  # telemetry reports both wheels at 0 m/s
    response = json.loads(nav.handle(b'{"op":"cmd","linear":0.1,"angular":0}\n'))
    assert not response["ok"] and "stall" in response["error"]
    assert not nav.owned and wheel_writes(uart)[-1] == {"T": 1, "L": 0.0, "R": 0.0}
    assert nav.dispatch({"op": "status"})["fault"]["reason"] == "stall_wheel_left"
    with pytest.raises(ValueError, match="re-arm"):
        nav.dispatch({"op": "cmd", "linear": 0.1, "angular": 0})

    nav.stall_monitor.seconds = 1.5
    write_arm(nav.root, id="session-2")
    assert nav.dispatch({"op": "cmd", "linear": 0.1, "angular": 0})["ok"]
    assert nav.dispatch({"op": "status"})["fault"] is None


def test_container_safety_stop_revokes_the_arm_session(rig: tuple[NavBridge, Mock]) -> None:
    nav, _ = rig
    write_arm(nav.root)
    nav.board.guard.heartbeat()
    assert nav.dispatch({"op": "cmd", "linear": 0.05, "angular": 0})["ok"]
    assert nav.dispatch({"op": "stop", "reason": "stall_body"}) == {"ok": True}
    assert not nav.owned and nav.fault is not None and nav.fault["reason"] == "stall_body"
    with pytest.raises(ValueError, match="re-arm"):
        nav.dispatch({"op": "cmd", "linear": 0.05, "angular": 0})
    with pytest.raises(ValidationError):
        nav.dispatch({"op": "stop", "reason": "because"})
    # A plain stop (e.g. Nav2 finished) does not fault the session.
    write_arm(nav.root, id="session-2")
    assert nav.dispatch({"op": "cmd", "linear": 0.05, "angular": 0})["ok"]
    assert nav.dispatch({"op": "stop"}) == {"ok": True}
    assert nav.fault is None


def test_probe_holds_one_factory_command_and_always_stops(rig: tuple[NavBridge, Mock]) -> None:
    nav, uart = rig
    request = {"op": "probe", "command": "t13", "a": 0.05, "b": 0.3, "seconds": 0.3}
    write_arm(nav.root)  # a normal arm is not enough
    with pytest.raises(ValueError, match="probe-mode"):
        nav.dispatch(request)
    write_arm(nav.root, id="probe-1", mode="probe", expires_at=time.time() + 8)
    with pytest.raises(ValueError, match="only allows probe"):
        nav.dispatch({"op": "cmd", "linear": 0.05, "angular": 0})
    nav.board.guard.heartbeat()
    uart.write.reset_mock()
    result = nav.dispatch(request)
    assert result["ok"] and result["frame"] == {"T": 13, "X": 0.05, "Z": 0.3}
    assert result["targets"]["L"] < 0.05 < result["targets"]["R"]
    sent = [json.loads(c.args[0]) for c in uart.write.call_args_list]
    last_t13 = max(i for i, c in enumerate(sent) if c.get("T") == 13)
    assert sum(c.get("T") == 13 for c in sent) >= 2
    assert {"T": 1, "L": 0.0, "R": 0.0} in sent[last_t13:]
    assert not nav.owned and result["samples"]
    with pytest.raises(ValueError, match="T13 limits"):
        nav.dispatch(request | {"a": 0.2})
    with pytest.raises(ValueError, match="T1 limits"):
        nav.dispatch(request | {"command": "t1", "a": 0.05, "b": -0.2})
    with pytest.raises(ValidationError):
        nav.dispatch(request | {"seconds": 3})
    write_arm(nav.root, id="probe-2", mode="probe", expires_at=time.time() + 30)
    with pytest.raises(ValueError, match="too long"):
        nav.dispatch(request)


def test_probe_stops_on_emergency_stop(rig: tuple[NavBridge, Mock]) -> None:
    nav, _ = rig
    write_arm(nav.root, id="probe-1", mode="probe", expires_at=time.time() + 8)
    nav.board.guard.heartbeat()
    nav.board.stop(emergency=True)
    with pytest.raises(ValueError, match="estop"):
        nav.dispatch({"op": "probe", "command": "t1", "a": 0.05, "b": 0.05, "seconds": 0.2})
