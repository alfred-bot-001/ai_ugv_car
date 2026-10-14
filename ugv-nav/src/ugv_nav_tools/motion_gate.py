"""Container-side motion gate in front of pi-agent, ported from the 2026-10-13 bridge.

pi-agent remains the final authority (arm, lease, watchdogs). This gate adds what only
the ROS side can see: fresh laser odometry, a fresh scan and the space ahead.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

STALE_SECONDS = 0.35
MIN_FRONT_RAYS = 10
FRONT_STOP_M = 0.40
NEAR_WALL_MAX_TURN = 0.15
TURN_CLEAR_RADIUS_M = 0.36
# base_lidar_link is yawed +90 deg on this car, so the robot front is -90 deg in the scan.
LIDAR_MOUNT_YAW = math.pi / 2
FRONT_HALF_WIDTH = math.pi / 4


@dataclass(frozen=True)
class Perception:
    scan_at: float
    front_count: int
    front_min: float
    laser_odom_at: float


@dataclass(frozen=True)
class Decision:
    """stop=True means send an explicit stop; otherwise send linear/angular."""

    linear: float
    angular: float
    stop: bool
    reason: str = ""


def front_ranges(
    ranges: Sequence[float],
    angle_min: float,
    angle_increment: float,
    mount_yaw: float = LIDAR_MOUNT_YAW,
    half_width: float = FRONT_HALF_WIDTH,
) -> list[float]:
    """Finite returns within +-half_width of the robot's forward axis."""
    found = []
    for index, distance in enumerate(ranges):
        bearing = angle_min + index * angle_increment + mount_yaw
        bearing = math.atan2(math.sin(bearing), math.cos(bearing))
        if abs(bearing) < half_width and math.isfinite(distance):
            found.append(distance)
    return found


def rotation_space_clear(
    data: Sequence[int],
    width: int,
    height: int,
    resolution: float,
    origin_x: float,
    origin_y: float,
    x: float,
    y: float,
    radius: float = TURN_CLEAR_RADIUS_M,
) -> bool:
    """True only if every map cell within the turning radius is known free (unknown blocks)."""
    numbers = (resolution, origin_x, origin_y, x, y, radius)
    if resolution <= 0 or len(data) != width * height or not all(map(math.isfinite, numbers)):
        return False
    reach = radius + resolution * math.sqrt(2) / 2
    x_min = math.floor((x - reach - origin_x) / resolution)
    x_max = math.floor((x + reach - origin_x) / resolution)
    y_min = math.floor((y - reach - origin_y) / resolution)
    y_max = math.floor((y + reach - origin_y) / resolution)
    for iy in range(y_min, y_max + 1):
        for ix in range(x_min, x_max + 1):
            cx = origin_x + (ix + 0.5) * resolution
            cy = origin_y + (iy + 0.5) * resolution
            if math.hypot(cx - x, cy - y) > reach:
                continue
            if not (0 <= ix < width and 0 <= iy < height) or data[iy * width + ix] != 0:
                return False
    return True


def gate(
    linear: float, angular: float, now: float, seen: Perception, turn_clear: Callable[[], bool]
) -> Decision:
    """Zero always passes so stopping never depends on perception."""
    if linear == 0 and angular == 0:
        return Decision(0.0, 0.0, stop=False)
    if now - seen.laser_odom_at > STALE_SECONDS:
        return Decision(0.0, 0.0, stop=True, reason="laser odometry stale or invalid")
    if now - seen.scan_at > STALE_SECONDS or seen.front_count < MIN_FRONT_RAYS:
        return Decision(0.0, 0.0, stop=True, reason="scan stale or too few valid front rays")
    if seen.front_min < FRONT_STOP_M:
        if angular and turn_clear():
            turn = max(-NEAR_WALL_MAX_TURN, min(NEAR_WALL_MAX_TURN, angular))
            return Decision(0.0, turn, stop=False, reason="near wall: verified-clear turn only")
        return Decision(0.0, 0.0, stop=True, reason="front < 0.40 m and turn space not verified")
    return Decision(linear, angular, stop=False)
