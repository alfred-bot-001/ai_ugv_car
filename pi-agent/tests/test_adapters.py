"""Hardware lifetime, driver freshness, watchdog stop and DSP handshake tests."""

import json
import mmap
import os
import struct
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from tvpet_pi.capture import Camera, CaptureWorker, Microphone
from tvpet_pi.hardware import Board
from tvpet_pi.m2 import M2Link, encode
from tvpet_pi.protocol import Stick
from tvpet_pi.safety import Guard


def test_v4l2_requeues_frames_and_rejects_stale_timestamp(tmp_path: Path, monkeypatch) -> None:
    device = tmp_path / "fake-video"
    jpeg = b"\xff\xd8frame\xff\xd9"
    page_size = mmap.ALLOCATIONGRANULARITY
    device.write_bytes(jpeg + bytes(page_size * 2 - len(jpeg)))
    stale = [False]
    queued = []

    def ioctl(fd, request, value):
        number = request & 255
        if number == 9:
            index = struct.unpack_from("I", value)[0]
            struct.pack_into("I", value, 72, page_size)
            struct.pack_into("I", value, 64, index * page_size)
        elif number == 17:
            stamp = time.monotonic_ns() - (3_000_000_000 if stale[0] else 0)
            struct.pack_into("IIII", value, 0, 0, 1, len(jpeg), 0x2000)
            struct.pack_into("qq", value, 24, stamp // 1_000_000_000, stamp % 1_000_000_000 // 1000)
            struct.pack_into("I", value, 56, 12)
        elif number == 15:
            queued.append(1)

    monkeypatch.setattr("tvpet_pi.capture.fcntl.ioctl", ioctl)
    camera = Camera(str(device))
    camera.open()
    frame = camera.read()
    assert frame.sequence == 12 and frame.jpeg == jpeg
    stale[0] = True
    with pytest.raises(ValueError, match="stale"):
        camera.read()
    assert len(queued) == 4  # Both reads return driver buffers, including the rejected frame.
    camera.close()
    assert camera.fd == -1 and camera.maps == []


def test_capture_requires_explicit_start_and_expires(monkeypatch) -> None:
    microphone = Microphone()
    opened, closed = [], []
    monkeypatch.setattr(microphone, "open", lambda: opened.append(1))
    monkeypatch.setattr(microphone, "close", lambda: closed.append(1))

    def read():
        time.sleep(0.003)
        return bytes(640)

    monkeypatch.setattr(microphone, "read", read)
    worker = CaptureWorker(microphone)
    assert not opened and not worker.active
    worker.start()
    time.sleep(0.015)
    worker.renew()
    assert worker.latest()[1] == bytes(640)
    worker.deadline = time.monotonic() - 1
    time.sleep(0.02)
    assert not worker.active and worker.latest()[1] is None and closed
    worker.stop()


def test_alsa_exact_format_and_cleanup(monkeypatch) -> None:
    process = Mock()
    process.stdout.read.return_value = bytes(640)
    popen = Mock(return_value=process)
    monkeypatch.setattr("tvpet_pi.capture.subprocess.Popen", popen)
    monkeypatch.setattr("tvpet_pi.capture.select.select", lambda *args: ([process.stdout], [], []))
    microphone = Microphone()
    microphone.open()
    args = popen.call_args[0][0]
    assert args[args.index("-f") + 1] == "S16_LE"
    assert args[args.index("-r") + 1] == "16000"
    assert microphone.read() == bytes(640)
    process.stdout.read.return_value = b""
    with pytest.raises(ValueError):
        microphone.read()
    microphone.close()
    process.terminate.assert_called_once()
    assert microphone.process is None


def test_uart_watchdog_and_evidence(tmp_path: Path, monkeypatch) -> None:
    uart = Mock()
    uart.in_waiting = 0
    uart.read.return_value = b'{"T":1001,"L":0,"R":0,"v":12.1,"pan":0,"tilt":0}\n'
    monkeypatch.setattr("tvpet_pi.hardware.serial.Serial", Mock(return_value=uart))
    boot_id = "test-boot"
    original = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, *args, **kwargs: (
            boot_id
            if str(self) == "/proc/sys/kernel/random/boot_id"
            else original(self, *args, **kwargs)
        ),
    )
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": 1,
                "port": "test-uart",
                "boot_id": boot_id,
                "expires_at": time.time() + 60,
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
    guard = Guard()
    board = Board("test-uart", guard, evidence)
    board.open()
    time.sleep(0.04)
    try:
        assert board.status()["wheels_ready"]
        guard.heartbeat()
        board.received_at = time.monotonic() - 0.8
        lease = board.acquire()
        assert time.monotonic() - board.received_at < 0.1
        board.move(Stick(target="wheels", x=0, y=0.5, lease=lease, sequence=1))

        def writes() -> list[dict[str, Any]]:
            return [json.loads(call.args[0]) for call in uart.write.call_args_list]

        assert any(c.get("T") == 1 and c.get("L") == 0.05 for c in writes())
        guard.heartbeat()
        time.sleep(0.25)  # the watchdog re-drives the held state under the 300 ms timeout
        assert sum(1 for c in writes() if c.get("T") == 1 and c.get("L") == 0.05) >= 2
        guard.heartbeat()
        written = len(writes())
        time.sleep(0.5)  # no new state: motion stops, the lease stays for the next state
        assert guard.lease == lease
        wheel = [c for c in writes() if c.get("T") == 1]
        assert wheel[-1] == {"T": 1, "L": 0.0, "R": 0.0}
        assert not any(c.get("T") == 135 for c in writes()[written:])  # no sagging stop
        guard.heartbeat()
        board.move(Stick(target="gimbal", x=1, y=0, lease=lease, sequence=2))
        time.sleep(0.15)  # integrated at 20 Hz: pan grows, tilt stays, limits hold
        gimbal = [c for c in writes() if c.get("T") == 133]
        assert gimbal and gimbal[-1]["X"] > gimbal[0]["X"] >= 0 and gimbal[-1]["Y"] == 0
        assert gimbal[-1]["SPD"] == 0 and all(c["X"] <= 170 for c in gimbal)
        board.stop(emergency=True)
        assert json.loads(uart.write.call_args_list[-1].args[0]) == {"T": 135}
        assert guard.estopped
        evidence.write_text("{}")
        board.load_evidence()
        assert not guard.wheels_verified
        with pytest.raises(ValueError):
            board.acquire()
    finally:
        board.close()
    uart.close.assert_called_once()


def test_m2_handshake_ack_and_transient_wakeup(monkeypatch) -> None:
    uart = Mock()
    calls = [encode(1, 42, b"\xa5\x00\x00\x00")]

    def read(count):
        time.sleep(0.005)
        return calls.pop(0) if calls else b""

    uart.read.side_effect = read
    monkeypatch.setattr("tvpet_pi.m2.serial.Serial", Mock(return_value=uart))
    m2 = M2Link()
    m2.open()
    time.sleep(0.02)
    m2.wake()
    m2.close()
    packets = [call.args[0] for call in uart.write.call_args_list]
    assert packets[0] == encode(255, 42, b"\xa5\x00\x00\x00")
    assert b"manual_wakeup" in packets[1] and b"setting" not in packets[1]
    assert m2.last_seen > 0


def test_gimbal_limits_follow_the_factory_range() -> None:
    """2026-10-23: pan +-170, tilt -30..90 (firmware allows +-180 / -30..90)."""
    board = Board("test-uart", Guard())
    board.serial = Mock()

    def targets(pan: float, tilt: float, x: float, y: float) -> tuple[float, float]:
        board.pan, board.tilt = pan, tilt
        now = time.monotonic()
        board.last_gimbal_at = now - 0.2
        stick = Stick(target="gimbal", x=x, y=y, lease="test-lease-0123456789", sequence=1)
        board._drive_gimbal(stick, now)
        command = json.loads(board.serial.write.call_args.args[0])
        return command["X"], command["Y"]

    assert targets(169.0, 0.0, 1, 0) == (170.0, 0.0)
    assert targets(-169.0, 0.0, -1, 0) == (-170.0, 0.0)
    assert targets(0.0, 89.0, 0, 1) == (0.0, 90.0)
    assert targets(0.0, -29.0, 0, -1) == (0.0, -30.0)
    assert targets(100.0, 60.0, 1, 0) == (112.0, 60.0)  # 60 deg/s * 0.2 s inside the range

    board.received_at = time.monotonic()
    board.telemetry = {"L": 0, "R": 0, "v": 12.1, "pan": -160.0, "tilt": 85.0}
    assert board.health_error() == ""
    board.telemetry["pan"] = 175.0
    assert board.health_error() == "云台反馈超出控制范围"
    board.telemetry["pan"], board.telemetry["tilt"] = 0.0, -35.0
    assert board.health_error() == "云台反馈超出控制范围"
