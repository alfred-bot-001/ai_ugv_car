"""Planar poses and patrol routes; pure so they can be validated before any motion."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_WAYPOINTS = 20
MAX_LOOPS = 10
# Nav2 result 4 plus this final check. 0.10 m (was 0.075) because the six-wheel skid
# chassis slides a few cm while turning in place (measured 2026-10-14).
GOAL_TOLERANCE_M = 0.10
# 2026-10-23, user-approved (was 0.18): wheels below 0.07 m/s stutter, so the slowest
# in-place turn is about 0.8 rad/s and the last 10-20 deg overshoot; a map-page goal
# ended 13 cm / 20 deg short. Matches Nav2's yaw_goal_tolerance in config/nav.yaml.
GOAL_TOLERANCE_RAD = 0.35


@dataclass(frozen=True)
class Pose:
    """x, y in metres and yaw in radians, in the map frame."""

    x: float
    y: float
    yaw: float

    def __post_init__(self) -> None:
        if not all(map(math.isfinite, (self.x, self.y, self.yaw))):
            raise ValueError("pose values must be finite")


def wrap(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


def yaw_to_quaternion(yaw: float) -> tuple[float, float]:
    """(z, w) of a rotation about the vertical axis."""
    return math.sin(yaw / 2), math.cos(yaw / 2)


def quaternion_to_yaw(z: float, w: float) -> float:
    return wrap(2 * math.atan2(z, w))


def goal_errors(reached: Pose, goal: Pose) -> tuple[float, float]:
    return math.hypot(reached.x - goal.x, reached.y - goal.y), abs(wrap(reached.yaw - goal.yaw))


def within_tolerance(reached: Pose, goal: Pose) -> bool:
    distance, heading = goal_errors(reached, goal)
    return distance <= GOAL_TOLERANCE_M and heading <= GOAL_TOLERANCE_RAD


@dataclass(frozen=True)
class Route:
    waypoints: tuple[Pose, ...]
    loops: int

    def legs(self) -> list[tuple[int, int, Pose]]:
        """(loop, index, pose) in driving order."""
        return [
            (loop, index, pose)
            for loop in range(1, self.loops + 1)
            for index, pose in enumerate(self.waypoints, start=1)
        ]


def parse_route(value: Any, loops: int | None = None) -> Route:
    """{"loops": 1, "waypoints": [{"x": 0, "y": 0, "yaw": 0}, ...]}"""
    if not isinstance(value, dict) or not isinstance(value.get("waypoints"), list):
        raise ValueError("route needs a waypoints list")
    points = value["waypoints"]
    if not 1 <= len(points) <= MAX_WAYPOINTS:
        raise ValueError(f"route needs 1..{MAX_WAYPOINTS} waypoints")
    poses = []
    for point in points:
        if not isinstance(point, dict) or set(point) - {"x", "y", "yaw", "name"}:
            raise ValueError("waypoint keys are x, y, yaw and optional name")
        try:
            poses.append(Pose(float(point["x"]), float(point["y"]), float(point.get("yaw", 0.0))))
        except (KeyError, TypeError) as error:
            raise ValueError("waypoint needs numeric x and y") from error
    count = loops if loops is not None else value.get("loops", 1)
    if not isinstance(count, int) or not 1 <= count <= MAX_LOOPS:
        raise ValueError(f"loops must be 1..{MAX_LOOPS}")
    return Route(tuple(poses), count)


def load_route(path: Path, loops: int | None = None) -> Route:
    return parse_route(json.loads(path.read_text()), loops)
