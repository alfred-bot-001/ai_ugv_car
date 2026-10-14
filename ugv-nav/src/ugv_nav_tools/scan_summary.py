"""Pure LaserScan statistics for N0 acceptance; no ROS imports so it is testable anywhere."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Sector:
    """Readings whose bearing falls in [start_deg, start_deg + width)."""

    start_deg: int
    nearest_m: float | None
    near_body_share: float
    invalid_share: float


def is_valid(distance: float) -> bool:
    """LD19 reports blocked or missing returns as 0, NaN or inf."""
    return math.isfinite(distance) and distance > 0


def rate_hz(stamps: Sequence[float]) -> float | None:
    """Mean message rate from monotonic receive times."""
    if len(stamps) < 2 or stamps[-1] <= stamps[0]:
        return None
    return (len(stamps) - 1) / (stamps[-1] - stamps[0])


def sectors(
    ranges: Sequence[float],
    angle_min: float,
    angle_increment: float,
    width_deg: int = 15,
    near_m: float = 0.30,
) -> list[Sector]:
    """Group a scan by bearing in the scan frame, 0 to 360 degrees counter-clockwise."""
    if not 0 < width_deg <= 360 or 360 % width_deg:
        raise ValueError("width_deg must divide 360")
    groups: dict[int, list[float]] = {}
    for index, distance in enumerate(ranges):
        bearing = math.degrees(angle_min + index * angle_increment) % 360
        start = int(bearing // width_deg) * width_deg % 360
        groups.setdefault(start, []).append(distance)
    result = []
    for start, values in sorted(groups.items()):
        valid = [d for d in values if is_valid(d)]
        result.append(
            Sector(
                start_deg=start,
                nearest_m=min(valid) if valid else None,
                near_body_share=sum(d < near_m for d in valid) / len(values),
                invalid_share=1 - len(valid) / len(values),
            )
        )
    return result


def occluded_arcs(found: Sequence[Sector], threshold: float = 0.5) -> list[tuple[int, int]]:
    """Merge adjacent sectors that are mostly body returns or invalid into [start, end) arcs."""
    if len(found) < 2:
        return []
    width = found[1].start_deg - found[0].start_deg
    arcs: list[tuple[int, int]] = []
    for sector in found:
        if sector.near_body_share + sector.invalid_share < threshold:
            continue
        if arcs and arcs[-1][1] == sector.start_deg:
            arcs[-1] = (arcs[-1][0], sector.start_deg + width)
        else:
            arcs.append((sector.start_deg, sector.start_deg + width))
    if len(arcs) > 1 and arcs[0][0] == 0 and arcs[-1][1] == 360:
        arcs[0] = (arcs[-1][0], arcs[0][1] + 360)
        arcs.pop()
    return arcs
