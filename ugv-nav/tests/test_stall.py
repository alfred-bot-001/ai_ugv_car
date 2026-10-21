"""Wheels-turning-but-body-still detection (S0 §3.1 B), tested without ROS."""

import math

import pytest

from ugv_nav_tools.stall import STALL_BODY_SECONDS, BodyStallWatch, Pose2D, Sample, stalled, wrap


def samples(wheel_step: float, laser_step: float, n: int = 11, turn: float = 0.0) -> list[Sample]:
    """n samples 0.2 s apart; wheel and laser odometry advance by fixed steps."""
    return [
        Sample(i / 5, Pose2D(wheel_step * i, 0.0, turn * i), Pose2D(laser_step * i, 0.0, 0.0))
        for i in range(n)
    ]


def test_wheels_moving_with_the_body_still_is_a_stall() -> None:
    assert stalled(samples(0.012, 0.0))  # 0.12 m of wheel travel over 2.0 s


def test_normal_driving_short_windows_and_crawling_are_not_stalls() -> None:
    assert not stalled(samples(0.012, 0.011))
    assert not stalled(samples(0.012, 0.0, n=10))  # spans only 1.8 s
    assert not stalled(samples(0.005, 0.0))  # 0.05 m, below the wheel travel threshold
    assert not stalled([])


def test_spinning_wheels_need_the_body_to_turn() -> None:
    spinning = samples(0.0, 0.0, turn=0.06)  # 0.6 rad of wheel yaw
    assert stalled(spinning)
    turning = [Sample(s.t, s.wheel, Pose2D(0.0, 0.0, 0.06 * i)) for i, s in enumerate(spinning)]
    assert not stalled(turning)


def test_yaw_wraps_across_pi() -> None:
    assert abs(wrap(3 * math.pi)) == pytest.approx(math.pi)
    jitter = [
        Sample(i / 5, Pose2D(0.0, 0.0, 3.14 if i % 2 else -3.14), Pose2D(0.0, 0.0, 0.0))
        for i in range(11)
    ]
    assert not stalled(jitter)  # 0.003 rad steps, not 6.28 rad jumps


def test_watch_keeps_one_window_and_ignores_duplicates() -> None:
    watch = BodyStallWatch()
    results = [watch.add(s) for s in samples(0.012, 0.0, n=30)]
    assert not any(results[:10]) and results[10] and results[-1]
    assert watch.samples[-1].t - watch.samples[0].t < STALL_BODY_SECONDS + 0.2 + 1e-9
    assert not watch.add(watch.samples[-1])
    watch.reset()
    assert not watch.samples
