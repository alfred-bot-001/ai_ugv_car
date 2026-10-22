"""Rejected stale/replayed commands must never reach a hardware writer."""

import json

import pytest
from pydantic import ValidationError
from tvpet_pi.hardware import parse_telemetry
from tvpet_pi.m2 import Decoder, encode
from tvpet_pi.protocol import Stick
from tvpet_pi.safety import HOLD_MAX_AGE, Guard, LeaseError, circle, differential


def test_circle_and_differential() -> None:
    assert circle(1, 1)[0] == pytest.approx(2**-0.5)
    assert differential(0, 1) == (0.1, 0.1)
    assert differential(1, 0) == (0.1, -0.1)
    assert max(map(abs, differential(1, 1))) <= 0.1
    assert differential(0, 0) == (0, 0)


def test_guard_holds_the_latest_state_and_stops_without_dropping_the_lease() -> None:
    now = [100.0]
    guard = Guard(lambda: now[0])
    guard.gimbal_verified = True
    guard.heartbeat()
    with pytest.raises(LeaseError):  # no lease yet: the browser leases, no toast
        guard.accept(Stick(target="gimbal", x=0, y=1, lease="x" * 16, sequence=1))
    lease = guard.acquire()
    stick = Stick(target="gimbal", x=0.2, y=0.1, lease=lease, sequence=1)
    assert guard.accept(stick) == (0.2, 0.1)
    assert guard.accept(stick) is None  # a late duplicate is ignored, never rejected
    assert guard.holding("gimbal") is stick and guard.holding("wheels") is None
    now[0] += HOLD_MAX_AGE - 0.01
    assert guard.holding("gimbal") is stick
    now[0] += 0.02  # the page stopped relaying: motion stops, the lease stays
    assert guard.holding("gimbal") is None and guard.lease == lease and not guard.expired()
    guard.heartbeat()
    newer = stick.model_copy(update={"sequence": 2})
    assert guard.accept(newer) == (0.2, 0.1) and guard.holding("gimbal") is newer
    with pytest.raises(LeaseError):
        guard.accept(stick.model_copy(update={"lease": "other-lease-0000", "sequence": 3}))
    now[0] += 0.61  # three missed Mini heartbeats end the lease
    assert guard.expired() and guard.holding("gimbal") is None
    old_epoch = guard.epoch
    guard.disconnect()
    assert guard.epoch != old_epoch and not guard.lease and not guard.held
    guard.release(emergency=True)
    with pytest.raises(ValueError):
        guard.acquire()
    guard.reset()
    guard.acquire()
    now[0] += 2.01
    guard.heartbeat()
    assert not guard.expired()
    guard.estopped = True
    with pytest.raises(ValueError, match="急停"):
        guard.accept(Stick(target="gimbal", x=0, y=1, lease=guard.lease, sequence=9))


def test_held_lease_keeps_driving_for_as_long_as_states_and_heartbeats_arrive() -> None:
    # 2026-10-22, user decision: no time budget; the car drives while states keep
    # coming. Only stale heartbeats end the lease.
    now = [500.0]
    guard = Guard(lambda: now[0])
    guard.wheels_verified = True
    guard.heartbeat()
    lease = guard.acquire()
    for sequence in range(1, 6001):  # 10 minutes at 10 states per second
        now[0] += 0.1
        guard.heartbeat()
        stick = Stick(target="wheels", x=0, y=1, lease=lease, sequence=sequence)
        assert guard.accept(stick) == (0, 1)
        assert guard.holding("wheels") is stick and not guard.expired()
    now[0] += 0.61
    assert guard.expired()  # the Board watchdog releases the lease and stops
    late = stick.model_copy(update={"sequence": 9999})
    assert guard.accept(late) == (0, 1) and guard.holding("wheels") is None


def test_nav_session_deadline_still_bounds_the_lease() -> None:
    now = [10.0]
    guard = Guard(lambda: now[0])
    guard.wheels_verified = True
    guard.heartbeat()
    lease = guard.acquire()
    guard.press_deadline = guard.lease_deadline = now[0] + 5
    assert guard.accept(Stick(target="wheels", x=0, y=1, lease=lease, sequence=1))
    now[0] += 5
    assert guard.expired()
    with pytest.raises(ValueError, match="到期"):
        guard.accept(Stick(target="wheels", x=0, y=1, lease=lease, sequence=2))


def test_wheels_hardware_gate_and_malformed_input() -> None:
    guard = Guard()
    guard.heartbeat()
    lease = guard.acquire()
    stick = Stick(target="wheels", x=0, y=1, lease=lease, sequence=1)
    with pytest.raises(ValueError, match="验收"):
        guard.accept(stick)
    with pytest.raises(ValidationError):
        Stick.model_validate({**stick.model_dump(), "x": float("nan")})
    with pytest.raises(ValidationError):
        Stick.model_validate({**stick.model_dump(), "angle": 90})
    with pytest.raises(ValidationError):  # the retired per-command challenge field
        Stick.model_validate({**stick.model_dump(), "challenge": "c" * 16})


def test_stick_speed_defaults_to_010_and_stays_within_the_joystick_band() -> None:
    base = {"target": "wheels", "x": 0, "y": 1, "lease": "l" * 16}
    assert Stick.model_validate({**base, "sequence": 1}).speed == 0.10
    assert Stick.model_validate({**base, "sequence": 1, "speed": 0.18}).speed == 0.18
    assert Stick.model_validate({**base, "sequence": 1, "speed": 0.07}).speed == 0.07
    for speed in (0.19, 0.06, 0, -0.1, float("nan")):
        with pytest.raises(ValidationError):
            Stick.model_validate({**base, "sequence": 1, "speed": speed})


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
