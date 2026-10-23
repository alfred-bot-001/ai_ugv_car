"""Frontier exploration targets on a SLAM occupancy grid (pure, no ROS).

A frontier cell is known free space next to unknown space. Neighbouring frontier cells
form a cluster; for each large enough cluster we pick a goal on known free space that
keeps CLEARANCE from walls (the motion gate stops 0.30 m before an obstacle ahead) and
that the robot can reach through known free space, then face the goal at the unknown
area so the LiDAR sees it. Nearest reachable targets come first.

Grids follow nav_msgs/OccupancyGrid: row 0 is the bottom row at the origin; values 0 free,
100 occupied, -1 unknown.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

FREE_MAX = 25  # SLAM Toolbox publishes 0 for free; anything up to this counts as free
OCCUPIED_MIN = 65
CLEARANCE_M = 0.40  # goal distance from walls: robot radius 0.17 plus the 0.30 m gate
ROBOT_RADIUS_M = 0.20  # passable cells for the reachability search
MIN_FRONTIER_M = 0.30  # shorter frontiers are scan noise
MIN_TRAVEL_M = 0.30  # closer targets are where the car already stands
SEARCH_RADIUS_M = 1.0  # how far from a frontier's centre a goal may sit
NEIGHBOURS_4 = ((1, 0), (-1, 0), (0, 1), (0, -1))
NEIGHBOURS_8 = NEIGHBOURS_4 + ((1, 1), (1, -1), (-1, 1), (-1, -1))


@dataclass(frozen=True)
class Grid:
    width: int
    height: int
    resolution: float
    origin_x: float
    origin_y: float
    data: Sequence[int]

    def cell(self, x: float, y: float) -> tuple[int, int] | None:
        col = math.floor((x - self.origin_x) / self.resolution)
        row = math.floor((y - self.origin_y) / self.resolution)
        return (col, row) if 0 <= col < self.width and 0 <= row < self.height else None

    def centre(self, col: int, row: int) -> tuple[float, float]:
        return (
            self.origin_x + (col + 0.5) * self.resolution,
            self.origin_y + (row + 0.5) * self.resolution,
        )

    def value(self, col: int, row: int) -> int:
        return int(self.data[row * self.width + col])

    def free(self, col: int, row: int) -> bool:
        return 0 <= self.value(col, row) <= FREE_MAX

    def inside(self, col: int, row: int) -> bool:
        return 0 <= col < self.width and 0 <= row < self.height


@dataclass(frozen=True)
class Target:
    x: float
    y: float
    yaw: float
    frontier_m: float  # length of the frontier (cells times resolution)
    travel_m: float  # path length through known free space

    def to_json(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in self.__dict__.items()}


def wall_distance(grid: Grid, max_cells: int) -> list[int]:
    """Cells (8-neighbour steps) to the nearest occupied cell, capped at max_cells."""
    far = max_cells
    distance = [far] * (grid.width * grid.height)
    queue: deque[tuple[int, int]] = deque()
    for row in range(grid.height):
        for col in range(grid.width):
            if grid.value(col, row) >= OCCUPIED_MIN:
                distance[row * grid.width + col] = 0
                queue.append((col, row))
    while queue:
        col, row = queue.popleft()
        step = distance[row * grid.width + col] + 1
        if step >= far:
            continue
        for dc, dr in NEIGHBOURS_8:
            c, r = col + dc, row + dr
            if grid.inside(c, r) and distance[r * grid.width + c] > step:
                distance[r * grid.width + c] = step
                queue.append((c, r))
    return distance


def reachable(grid: Grid, start: tuple[int, int], passable: Sequence[bool]) -> dict[int, float]:
    """Path length in metres (8-neighbour, diagonal sqrt 2) to each reachable cell."""
    index = start[1] * grid.width + start[0]
    cost: dict[int, float] = {index: 0.0}
    queue: deque[tuple[int, int]] = deque([start])
    while queue:
        col, row = queue.popleft()
        here = cost[row * grid.width + col]
        for dc, dr in NEIGHBOURS_8:
            c, r = col + dc, row + dr
            if not grid.inside(c, r):
                continue
            i = r * grid.width + c
            if not passable[i]:
                continue
            step = here + (math.sqrt(2) if dc and dr else 1.0) * grid.resolution
            if step < cost.get(i, math.inf) - 1e-9:
                cost[i] = step
                queue.append((c, r))
    return cost


def frontier_clusters(grid: Grid) -> list[list[tuple[int, int]]]:
    """Free cells touching unknown space, grouped by 8-neighbour connectivity."""
    frontier: set[tuple[int, int]] = set()
    for row in range(grid.height):
        for col in range(grid.width):
            if not grid.free(col, row):
                continue
            for dc, dr in NEIGHBOURS_4:
                c, r = col + dc, row + dr
                if grid.inside(c, r) and grid.value(c, r) < 0:
                    frontier.add((col, row))
                    break
    clusters = []
    while frontier:
        seed = frontier.pop()
        cluster, queue = [seed], deque([seed])
        while queue:
            col, row = queue.popleft()
            for dc, dr in NEIGHBOURS_8:
                neighbour = (col + dc, row + dr)
                if neighbour in frontier:
                    frontier.remove(neighbour)
                    cluster.append(neighbour)
                    queue.append(neighbour)
        clusters.append(cluster)
    return clusters


def candidates(
    grid: Grid,
    robot: tuple[float, float],
    *,
    avoid: Iterable[tuple[float, float]] = (),
    avoid_radius_m: float = 0.3,
    limit: int = 10,
) -> list[Target]:
    """Reachable exploration targets, nearest first."""
    start = grid.cell(*robot)
    if start is None:
        return []
    clearance = math.ceil(CLEARANCE_M / grid.resolution)
    radius = math.ceil(ROBOT_RADIUS_M / grid.resolution)
    walls = wall_distance(grid, clearance + 1)
    passable = [
        grid.free(i % grid.width, i // grid.width) and walls[i] >= radius
        for i in range(grid.width * grid.height)
    ]
    passable[start[1] * grid.width + start[0]] = True  # the car may stand near a wall
    cost = reachable(grid, start, passable)
    avoided = list(avoid)
    search = math.ceil(SEARCH_RADIUS_M / grid.resolution)
    found = []
    for cluster in frontier_clusters(grid):
        length = len(cluster) * grid.resolution
        if length < MIN_FRONTIER_M:
            continue
        cx = sum(c for c, _ in cluster) / len(cluster)
        cy = sum(r for _, r in cluster) / len(cluster)
        best: tuple[float, int] | None = None
        for row in range(max(0, int(cy) - search), min(grid.height, int(cy) + search + 1)):
            for col in range(max(0, int(cx) - search), min(grid.width, int(cx) + search + 1)):
                i = row * grid.width + col
                if i not in cost or walls[i] < clearance:
                    continue
                gap = math.hypot(col - cx, row - cy)
                if gap <= search and (best is None or gap < best[0]):
                    best = (gap, i)
        if best is None:
            continue
        col, row = best[1] % grid.width, best[1] // grid.width
        travel = cost[best[1]]
        x, y = grid.centre(col, row)
        if travel < MIN_TRAVEL_M:
            continue
        if any(math.hypot(x - ax, y - ay) < avoid_radius_m for ax, ay in avoided):
            continue
        fx, fy = grid.centre(round(cx), round(cy))
        yaw = math.atan2(fy - y, fx - x) if math.hypot(fx - x, fy - y) > 1e-6 else 0.0
        found.append(Target(x, y, yaw, length, travel))
    found.sort(key=lambda t: (t.travel_m, -t.frontier_m))
    return found[:limit]


def explored_m2(grid: Grid) -> float:
    free = sum(1 for v in grid.data if 0 <= v <= FREE_MAX)
    return free * grid.resolution**2
