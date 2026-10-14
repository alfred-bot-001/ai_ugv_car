"""Rejected stale/replayed commands must never reach a hardware writer."""

import json

import pytest
from pydantic import ValidationError
from tvpet_pi.hardware import parse_telemetry
from tvpet_pi.m2 import Decoder, encode
from tvpet_pi.protocol import Stick
from tvpet_pi.safety import Guard, circle, differential


def test_circle_and_differential() -> None:
    assert circle(1, 1)[0] == pytest.approx(2**-0.5)
    assert differential(0, 1) == (0.1, 0.1)
    assert differential(1, 0) == (0.1, -0.1)
    assert max(map(abs, differential(1, 1))) <= 0.1
    assert differential(0, 0) == (0, 0)


def test_guard_stale_replay_press_and_disconnect() -> None:
    now = [100.0]
    guard = Guard(lambda: now[0])
    guard.gimbal_verified = True
    guard.heartbeat()
    before_lease = guard.challenge()
    lease = guard.acquire()
    assert before_lease not in guard.challenges
    challenge = guard.challenge()
    stick = Stick(target="gimbal", x=0.2, y=0.1, lease=lease, challenge=challenge, sequence=1)
    assert guard.accept(stick) == (0.2, 0.1)
    with pytest.raises(ValueError):
        guard.accept(stick)
    now[0] += 0.46
    assert guard.expired()
    with pytest.raises(ValueError):
        guard.accept(stick.model_copy(update={"sequence": 2}))
    old_epoch = guard.epoch
    guard.disconnect()
    assert guard.epoch != old_epoch and not guard.lease
    guard.release(emergency=True)
    with pytest.raises(ValueError):
        guard.acquire()
    guard.reset()
    guard.acquire()
    now[0] += 2.01
    guard.heartbeat()
    assert guard.expired()


def test_wheels_hardware_gate_and_malformed_input() -> None:
    guard = Guard()
    guard.heartbeat()
    lease = guard.acquire()
    stick = Stick(target="wheels", x=0, y=1, lease=lease, challenge=guard.challenge(), sequence=1)
    with pytest.raises(ValueError):
        guard.accept(stick)
    with pytest.raises(ValidationError):
        Stick.model_validate({**stick.model_dump(), "x": float("nan")})
    with pytest.raises(ValidationError):
        Stick.model_validate({**stick.model_dump(), "angle": 90})


def test_telemetry_resynchronizes_and_rejects_corrupt_values() -> None:
    assert parse_telemetry(b'boot noise\xff{"T":1001,"L":0,"R":0,"v":12.2}') == {
        "T": 1001,
        "L": 0,
        "R": 0,
        "v": 12.2,
    }
    assert (
        parse_telemetry(json.dumps({"T": 1001, "L": float("nan"), "R": 0, "v": 12}).encode())
        is None
    )
    assert parse_telemetry(b"not json") is None
    assert parse_telemetry(b'{"T":1}') is None
    assert parse_telemetry(b'{"T":1001,}') is None
    assert parse_telemetry(b"x" * 4097) is None


def test_m2_split_packets_checksum_and_overflow() -> None:
    decoder = Decoder()
    packet = encode(1, 200, b"\xa5\x00\x00\x00")
    assert decoder.feed(b"noise" + packet[:4]) == []
    assert decoder.feed(packet[4:]) == [(1, 200, b"\xa5\x00\x00\x00")]
    broken = packet[:-1] + bytes([packet[-1] ^ 1])
    assert decoder.feed(broken) == []
    assert decoder.feed(b"\xa5\x01\x04\xff\xff\x00\x00\x00" + packet) == [
        (1, 200, b"\xa5\x00\x00\x00")
    ]
