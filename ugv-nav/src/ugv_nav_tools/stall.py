"""Wheels turning but body still: wheel odometry against laser odometry (S0 §3.1 B).

Covers what the Pi's single-wheel check cannot see: both wheels spinning or slipping
on a low obstacle while the scan-matched pose does not move. Pure logic, no ROS.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

# Initial values, to be calibrated on the car (docs/task-autonomy-design.md §3.1, S0-2).
STALL_BODY_SECONDS = 2.0
WHEEL_TRAVEL_M = 0.10
WHEEL_TURN_RAD = 0.5
BODY_MOVE_M = 0.03
BODY_TURN_RAD = 0.08


@dataclass(frozen=True)
class Pose2D:
    x: float
    y: float
    yaw: float


@dataclass(frozen=True)
class Sample:
    """Wheel and laser odometry poses observed at the same monotonic time."""

    t: float
    wheel: Pose2D
    laser: Pose2D


def wrap(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


def stalled(window: Sequence[Sample], seconds: float = STALL_BODY_SECONDS) -> bool:
    """True when the window spans `seconds`, the wheels moved and the body did not."""
    if len(window) < 2 or window[-1].t - window[0].t < seconds:
        return False
    travel = turn = 0.0
    for before, after in zip(window, window[1:], strict=False):
        travel += math.hypot(after.wheel.x - before.wheel.x, after.wheel.y - before.wheel.y)
        turn += abs(wrap(after.wheel.yaw - before.wheel.yaw))
    first, last = window[0].laser, window[-1].laser
    moved = math.hypot(last.x - first.x, last.y - first.y)
    turned = abs(wrap(last.yaw - first.yaw))
    wheels_moving = travel >= WHEEL_TRAVEL_M or turn >= WHEEL_TURN_RAD
    return wheels_moving and moved < BODY_MOVE_M and turned < BODY_TURN_RAD


class BodyStallWatch:
    """Keeps just enough samples to cover the window and reports a stall."""

    def __init__(self, seconds: float = STALL_BODY_SECONDS) -> None:
        self.seconds = seconds
        self.samples: deque[Sample] = deque()

    def reset(self) -> None:
        self.samples.clear()

    def add(self, sample: Sample) -> bool:
        if self.samples and sample.t <= self.samples[-1].t:
            return False  # out of order or duplicate; keep the window monotonic
        self.samples.append(sample)
        # Drop old samples but keep one at or before the window start.
        while len(self.samples) > 1 and sample.t - self.samples[1].t >= self.seconds:
            self.samples.popleft()
        return stalled(list(self.samples), self.seconds)
