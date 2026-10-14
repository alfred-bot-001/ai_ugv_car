"""Saved qualification survives reboot, while control follows current hardware health."""

import json
import os
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from tvpet_pi.hardware import RUNAWAY_SPEED, Board
from tvpet_pi.protocol import Stick
from tvpet_pi.safety import Guard


@pytest.mark.parametrize(
    "changes,ready",
    [
        ({}, True),
        ({"release_policy": "timed"}, False),
        ({"boot_id": "rebooted"}, False),
        ({"boot_id": "rebooted", "release_policy": "persistent"}, True),
        ({"operator_released_at": 0}, False),
        ({"operator_released_at": float("inf")}, False),
        ({"operator_released_at": "bad"}, False),
        ({"verified_at": "bad"}, False),
        ({"port": "other-port"}, False),
        ({"schema": 2}, False),
        ({"firmware_timeout_ms": 1000}, False),
        ({"measured_stop_seconds": 2}, False),
        ({"max_command": 1}, False),
        ({"wheels_verified": False}, False),
    ],
)
def test_release_cannot_bypass_boot_and_wheel_evidence(
    tmp_path: Path, monkeypatch, changes: dict, ready: bool
) -> None:
    original = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *a, **kw: (
            "current-boot"
            if str(path) == "/proc/sys/kernel/random/boot_id"
            else original(path, *a, **kw)
        ),
    )
    now = time.time()
    record = {
        "schema": 1,
        "boot_id": "current-boot",
        "port": "test-port",
        "verified_at": now - 86400,
        "expires_at": now - 3600,
        "release_policy": "current_boot",
        "operator_released_at": now - 1,
        "wheels_verified": True,
        "gimbal_verified": True,
        "unit_profile": "t1_scalar_limited",
        "max_command": 0.1,
        "firmware_timeout_ms": 300,
        "measured_stop_seconds": 0.463,
        **changes,
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(record))
    os.chmod(path, 0o600)
    guard = Guard()
    board = Board("test-port", guard, path)
    board.load_evidence()
    assert guard.wheels_verified is ready
    assert board.evidence["verified_at"] == record["verified_at"]
    assert board.status()["gate_reason"]
    os.chmod(path, 0o644)
    board.load_evidence()
    assert not guard.wheels_verified and not guard.gimbal_verified
    os.chmod(path, 0o600)
    path.write_text("[]")
    board.load_evidence()
    assert not guard.wheels_verified and not guard.gimbal_verified


def test_persistent_qualification_checks_current_health_and_recovers(tmp_path: Path, monkeypatch):
    original = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *a, **kw: (
            "new-boot"
            if str(path) == "/proc/sys/kernel/random/boot_id"
            else original(path, *a, **kw)
        ),
    )
    path = tmp_path / "evidence.json"
    record = {
        "schema": 1,
        "boot_id": "old-boot",
        "port": "test-port",
        "verified_at": 100,
        "expires_at": 101,
        "release_policy": "persistent",
        "operator_released_at": 102,
        "wheels_verified": True,
        "gimbal_verified": True,
        "unit_profile": "t1_scalar_limited",
        "max_command": 0.1,
        "firmware_timeout_ms": 300,
        "measured_stop_seconds": 0.463,
    }
    path.write_text(json.dumps(record))
    os.chmod(path, 0o600)
    board = Board("test-port", Guard(), path)
    uart = Mock()
    board.serial = uart
    good = {"T": 1001, "L": 0, "R": 0, "v": 12.2, "pan": 0, "tilt": 0}
    board.telemetry = good
    board.received_at = time.monotonic()
    assert board.status()["wheels_ready"] and board.status()["gimbal_ready"]
    assert board.evidence["boot_id"] == "old-boot"  # Keep the actual original test provenance.
    board.received_at = time.monotonic() - 2
    assert not board.status()["wheels_ready"]
    assert board.status()["control_qualified"]
    board.received_at = time.monotonic()
    for changes in (
        {"v": 0},
        {"L": RUNAWAY_SPEED + 0.01},
        {"pan": 90},
        {"tilt": float("nan")},
        {"pan": None},
    ):
        board.telemetry = {**good, **changes}
        assert not board.status()["wheels_ready"] and not board.status()["gimbal_ready"]
        with pytest.raises(ValueError):
            board.move(
                Stick(
                    target="wheels",
                    x=1,
                    y=0,
                    lease="invalid-lease-000",
                    challenge="invalid-challenge",
                    sequence=1,
                )
            )
        assert not board.guard.estopped
    board.telemetry = good
    board.received_at = time.monotonic()
    assert board.status()["wheels_ready"] and board.status()["gimbal_ready"]
    board.guard.estopped = True
    assert board.status()[
        "estopped"
    ]  # Hardware recovery does not release the user's emergency stop.
    commands = [json.loads(call.args[0]) for call in uart.write.call_args_list]
    assert all(
        command.get("T") == 135 or command.get("L") == command.get("R") == 0 for command in commands
    )
    monkeypatch.setattr("tvpet_pi.hardware.time.time", lambda: 1)
    assert board.status()["control_qualified"]  # Startup clock correction is not a hardware fault.
