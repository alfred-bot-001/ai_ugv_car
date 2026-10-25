"""Localization health (S0 §3.2): map->odom jumps, scan-to-map match, AMCL covariance.

Pure logic, no ROS. The ugv-loc-health node feeds it and stops the car through
pi-agent when any check fails. S0 only stops and reports; it never relocalizes.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from .stall import Pose2D, wrap

# A jump is measured where the robot is, not at the odom origin: a heading fix of
# `yaw` swings the origin by distance * yaw, and 2026-10-25 that turned ordinary
# 0.1-0.4 rad SLAM fixes 2-4 m into a mapping run into 0.4-1.5 m "jumps" that halted
# the auto-explore. Localization thresholds (about 2.5x the goal tolerance) are the
# original ones; the 2026-10-14 incident jumped about 0.9 m.
JUMP_WINDOW_S = 1.0
JUMP_TRANSLATION_M = 0.25
JUMP_ROTATION_RAD = 0.35
# Mapping mode (2026-10-25): SLAM Toolbox corrects map->odom with every scan match,
# so a 0.4 rad heading fix is routine (seen at every mapping start, RF2O under-reads
# in-place turns) and must not stop the car. 0.5 m / 0.6 rad still catches the
# 0.9 m 2026-10-14 jump. Changing either value needs the user's approval.
MAPPING_JUMP_TRANSLATION_M = 0.5
MAPPING_JUMP_ROTATION_RAD = 0.6
MATCH_MIN = 0.45  # share of scan endpoints on occupied map cells (1-cell tolerance)
MATCH_SECONDS = 2.0
MATCH_TOLERANCE_CELLS = 1
OCCUPIED = 65  # occupancy value; matches map_saver's 0.65 occupied threshold
STD_XY_MAX_M = 0.30
STD_YAW_MAX_RAD = 0.35


@dataclass(frozen=True)
class Stamped:
    """A map->odom transform and where the robot was in the odom frame at that time."""

    t: float
    pose: Pose2D
    robot: tuple[float, float] = (0.0, 0.0)


def transform_point(pose: Pose2D, x: float, y: float) -> tuple[float, float]:
    """A point of the child frame expressed in the parent frame of `pose`."""
    c, s = math.cos(pose.yaw), math.sin(pose.yaw)
    return pose.x + c * x - s * y, pose.y + s * x + c * y


class JumpWatch:
    """A map->odom correction that moves the robot on the map more than a smooth fix
    would within one second.

    Pre-jump samples stay in the window for a second, so a jump keeps being reported
    until a full quiet window has passed.
    """

    def __init__(
        self, translation_m: float = JUMP_TRANSLATION_M, rotation_rad: float = JUMP_ROTATION_RAD
    ) -> None:
        self.translation_m = translation_m
        self.rotation_rad = rotation_rad
        self.samples: deque[Stamped] = deque()
        self.last: str | None = None

    def reset(self) -> None:
        self.samples.clear()
        self.last = None

    def add(self, sample: Stamped) -> str | None:
        if self.samples and sample.t <= self.samples[-1].t:
            return self.last
        while self.samples and sample.t - self.samples[0].t > JUMP_WINDOW_S:
            self.samples.popleft()
        reason = None
        rx, ry = sample.robot
        now_x, now_y = transform_point(sample.pose, rx, ry)
        for old in self.samples:
            was_x, was_y = transform_point(old.pose, rx, ry)
            moved = math.hypot(now_x - was_x, now_y - was_y)
            turned = abs(wrap(sample.pose.yaw - old.pose.yaw))
            if moved > self.translation_m or turned > self.rotation_rad:
                reason = f"map->odom jumped {moved:.2f} m / {turned:.2f} rad within 1 s"
                break
        self.samples.append(sample)
        self.last = reason
        return reason


def scan_endpoints(
    ranges: Sequence[float],
    angle_min: float,
    angle_increment: float,
    sensor: Pose2D,
    range_min: float,
    range_max: float,
) -> list[tuple[float, float]]:
    """Valid returns of one scan, in the map frame, given the sensor pose in the map."""
    points = []
    for index, distance in enumerate(ranges):
        if not math.isfinite(distance) or not range_min <= distance < range_max:
            continue
        angle = sensor.yaw + angle_min + index * angle_increment
        points.append(
            (sensor.x + distance * math.cos(angle), sensor.y + distance * math.sin(angle))
        )
    return points


def match_ratio(
    points: Sequence[tuple[float, float]],
    data: Sequence[int],
    width: int,
    height: int,
    resolution: float,
    origin_x: float,
    origin_y: float,
    tolerance: int = MATCH_TOLERANCE_CELLS,
) -> float | None:
    """Share of points with an occupied cell within `tolerance` cells; None without points."""
    if not points:
        return None
    hits = 0
    for x, y in points:
        cx = math.floor((x - origin_x) / resolution)
        cy = math.floor((y - origin_y) / resolution)
        found = False
        for dy in range(-tolerance, tolerance + 1):
            for dx in range(-tolerance, tolerance + 1):
                gx, gy = cx + dx, cy + dy
                if 0 <= gx < width and 0 <= gy < height and data[gy * width + gx] >= OCCUPIED:
                    found = True
                    break
            if found:
                break
        hits += found
    return hits / len(points)


class MatchWatch:
    """Scan-to-map match below MATCH_MIN for MATCH_SECONDS (localization mode only)."""

    def __init__(self) -> None:
        self.low_since: float | None = None

    def reset(self) -> None:
        self.low_since = None

    def add(self, t: float, ratio: float | None) -> str | None:
        if ratio is None or ratio >= MATCH_MIN:
            self.low_since = None
            return None
        if self.low_since is None:
            self.low_since = t
        if t - self.low_since >= MATCH_SECONDS:
            return f"scan matches the map only {ratio:.0%}"
        return None


def covariance_fault(covariance: Sequence[float]) -> str | None:
    """AMCL pose covariance (6x6, row-major) too wide to trust."""
    std_x, std_y, std_yaw = (math.sqrt(max(0.0, covariance[i])) for i in (0, 7, 35))
    if max(std_x, std_y) > STD_XY_MAX_M or std_yaw > STD_YAW_MAX_RAD:
        return f"AMCL uncertainty {max(std_x, std_y):.2f} m / {std_yaw:.2f} rad"
    return None


class LocalizationHealth:
    """Combines the checks; `reason` is the most recent failing check, None when healthy."""

    def __init__(self, localization: bool) -> None:
        self.localization = localization
        self.jumps = (
            JumpWatch()
            if localization
            else JumpWatch(MAPPING_JUMP_TRANSLATION_M, MAPPING_JUMP_ROTATION_RAD)
        )
        self.match = MatchWatch()
        self.reasons: dict[str, str | None] = {"jump": None, "match": None, "covariance": None}

    @property
    def reason(self) -> str | None:
        return next((r for r in self.reasons.values() if r), None)

    def on_transform(self, sample: Stamped) -> None:
        self.reasons["jump"] = self.jumps.add(sample)

    def on_match(self, t: float, ratio: float | None) -> None:
        if self.localization:
            self.reasons["match"] = self.match.add(t, ratio)

    def on_covariance(self, covariance: Sequence[float]) -> None:
        if self.localization:
            self.reasons["covariance"] = covariance_fault(covariance)


def should_halt(reason: str | None, board: dict[str, object]) -> bool:
    """Stop only while navigation drives the chassis and no safety stop is latched yet.

    2026-10-23: in mapping mode SLAM corrects map->odom by 0.3-0.5 m while the operator
    drives by hand; halting then stopped the joystick and left a stale fault on the page.
    The car is only at risk from bad localization while Nav2 is steering it.
    """
    fault = board.get("fault")
    return bool(reason) and board.get("nav_owned") is True and not isinstance(fault, dict)
