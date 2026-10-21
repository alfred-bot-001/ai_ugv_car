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

# Initial thresholds (about 2.5x the goal tolerance); to be checked against the
# S0-4 patrol recordings. The 2026-10-14 incident jumped about 0.9 m.
JUMP_WINDOW_S = 1.0
JUMP_TRANSLATION_M = 0.25
JUMP_ROTATION_RAD = 0.35
MATCH_MIN = 0.45  # share of scan endpoints on occupied map cells (1-cell tolerance)
MATCH_SECONDS = 2.0
MATCH_TOLERANCE_CELLS = 1
OCCUPIED = 65  # occupancy value; matches map_saver's 0.65 occupied threshold
STD_XY_MAX_M = 0.30
STD_YAW_MAX_RAD = 0.35


@dataclass(frozen=True)
class Stamped:
    t: float
    pose: Pose2D


class JumpWatch:
    """A map->odom correction larger than a smooth fix within one second.

    Pre-jump samples stay in the window for a second, so a jump keeps being reported
    until a full quiet window has passed.
    """

    def __init__(self) -> None:
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
        for old in self.samples:
            moved = math.hypot(sample.pose.x - old.pose.x, sample.pose.y - old.pose.y)
            turned = abs(wrap(sample.pose.yaw - old.pose.yaw))
            if moved > JUMP_TRANSLATION_M or turned > JUMP_ROTATION_RAD:
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
        self.jumps = JumpWatch()
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
