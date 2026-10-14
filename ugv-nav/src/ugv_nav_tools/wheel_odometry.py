"""Differential odometry from the Rover's cumulative wheel distances (diagnostic only).

On 老三 the firmware reports odl/odr in metres and L/R in m/s (measured 2026-10-13),
not the centimetres the vendor ROS driver assumes. Wheel yaw on the six-wheel skid
chassis disagrees badly with laser yaw, so the EKF does not fuse this output.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TRACK_WIDTH_M = 0.17452
# A larger per-sample jump means a firmware counter reset or corrupt sample.
MAX_STEP_M = 0.5


@dataclass(frozen=True)
class Pose2D:
    x: float
    y: float
    yaw: float
    linear: float
    angular: float


class WheelOdometry:
    def __init__(self, track_width: float = TRACK_WIDTH_M) -> None:
        self.track_width = track_width
        self.x = self.y = self.yaw = 0.0
        self.last: tuple[float, float] | None = None
        self.rejected = 0

    def update(self, left_m: float, right_m: float, left_mps: float, right_mps: float) -> Pose2D:
        values = (left_m, right_m, left_mps, right_mps)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("non-finite wheel telemetry")
        if self.last is not None:
            d_left, d_right = left_m - self.last[0], right_m - self.last[1]
            if max(abs(d_left), abs(d_right)) > MAX_STEP_M:
                self.rejected += 1
            else:
                distance = (d_left + d_right) / 2
                d_yaw = (d_right - d_left) / self.track_width
                heading = self.yaw + d_yaw / 2
                self.x += distance * math.cos(heading)
                self.y += distance * math.sin(heading)
                self.yaw = math.atan2(math.sin(self.yaw + d_yaw), math.cos(self.yaw + d_yaw))
        self.last = (left_m, right_m)
        return Pose2D(
            self.x,
            self.y,
            self.yaw,
            (left_mps + right_mps) / 2,
            (right_mps - left_mps) / self.track_width,
        )
