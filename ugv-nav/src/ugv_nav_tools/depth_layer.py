"""pi-agent's front depth scan, checked and turned into LaserScan ranges for Nav2.

pi-agent (tvpet_pi.depth_scan) sends bearings around base_link in mm: an obstacle 5-35 cm
above the floor within 1.5 m, 0 when that way was seen and is clear, None when it was not
seen. Nav2's depth_layer (nav.yaml) reads inf as clear out to range_max (inf_is_valid)
and drops NaN, so an unseen bearing neither clears nor marks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

FRAME = "base_link"
MAX_BEARINGS = 360
MAX_AGE_S = 2.0
MAX_RANGE_M = 10.0


@dataclass(frozen=True)
class DepthScan:
    sequence: int
    age: float  # seconds since the frame was read
    angle_min: float
    angle_increment: float
    range_max: float
    ranges: list[float]  # metres; inf clear, nan unknown

    @property
    def angle_max(self) -> float:
        return self.angle_min + self.angle_increment * (len(self.ranges) - 1)


def number(value: Any, low: float, high: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"depth scan {name} is not a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"depth scan {name} is out of range")
    return float(value)


def parse_depth_scan(value: Any) -> DepthScan | None:
    """None when pi-agent has no scan; ValueError when one is malformed."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("depth scan is not an object")
    sequence = value.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise ValueError("depth scan sequence is not an integer")
    age = number(value.get("age"), 0.0, MAX_AGE_S, "age")
    angle_min = number(value.get("angle_min"), -math.pi, 0.0, "angle_min")
    increment = number(value.get("angle_increment"), 1e-4, 0.2, "angle_increment")
    range_max = number(value.get("range_max"), 0.1, MAX_RANGE_M, "range_max")
    raw = value.get("ranges_mm")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_BEARINGS:
        raise ValueError("depth scan ranges are missing")
    if angle_min + increment * (len(raw) - 1) > math.pi:
        raise ValueError("depth scan bearings exceed half a turn")
    ranges = []
    for mm in raw:
        if mm is None:
            ranges.append(math.nan)
        elif mm == 0 and not isinstance(mm, bool):
            ranges.append(math.inf)
        else:
            ranges.append(number(mm, 1, range_max * 1000, "range") / 1000)
    return DepthScan(sequence, age, angle_min, increment, range_max, ranges)
