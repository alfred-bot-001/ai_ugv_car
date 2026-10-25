"""Frontier exploration targets on a SLAM occupancy grid (pure, no ROS).

A frontier cell is known free space next to unknown space. Neighbouring frontier cells
form a cluster, cut into 1 m pieces. Around each piece we sample viewpoints on known
free space that keep CLEARANCE from walls (the motion gate stops 0.30 m before an
obstacle ahead) and that the robot can reach through known free space, and keep the one
that would newly see the most through that piece (information gain), facing it. Pieces
that open up too little (a pocket under a cupboard) are dropped. The rest are ordered by
the shortest open tour from the robot through all of them (a travelling-salesman path,
exact for up to 10), and the first of that tour is the next goal; while driving there,
the leg ends once that piece's frontier is gone (frontier_left). Gain, the global tour
and replanning on a covered frontier follow FUEL (Zhou et al., RA-L 2021) and TARE (Cao
et al., RSS 2021), cut down to a 2D grid (2026-10-25, user request); before that the
nearest reachable target came first and every leg drove all the way.

Grids follow nav_msgs/OccupancyGrid: row 0 is the bottom row at the origin; values 0 free,
100 occupied, -1 unknown.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

FREE_MAX = 25  # SLAM Toolbox publishes 0 for free; anything up to this counts as free
OCCUPIED_MIN = 65
# Goal distance from walls: the motion gate stops 0.30 m before an obstacle ahead, so a
# goal 0.35 m off a wall is reached within the 0.10 m goal tolerance (0.40 left almost
# no goals in a cluttered study, 2026-10-23).
CLEARANCE_M = 0.35
ROBOT_RADIUS_M = 0.20  # passable cells for the reachability search
MIN_FRONTIER_M = 0.30  # shorter frontiers are scan noise
MIN_TRAVEL_M = 0.30  # closer targets are where the car already stands
SEARCH_RADIUS_M = 1.0  # how far from a frontier's centre a goal may sit
# A first scan leaves one frontier ringing the car; its centre is the car itself. Long
# frontiers are cut into 1 m tiles so each piece gets its own target facing it.
SEGMENT_M = 1.0
FACE_MIN_M = 0.15  # a frontier closer than this gives no useful heading
# Information gain: unknown cells a 360 deg scan from the viewpoint would cross through
# its frontier piece, within SENSOR_RANGE_M, until a wall or known space again. Unknown
# counts as see-through and the map's edge does not stop a ray (what lies beyond the map
# was never scanned), as in FUEL. The LD19 reaches 12 m; 4 m is what a home room scan
# adds reliably.
SENSOR_RANGE_M = 4.0
GAIN_RAYS = 72  # every 5 deg
MIN_GAIN_M2 = 0.25  # a piece that opens up less is not worth a leg
# Viewpoints sampled around a frontier piece: up to one per distance band and direction.
VIEWPOINT_BAND_M = 0.35
VIEWPOINT_DIRECTIONS = 8
VIEWPOINT_TRAVEL_WEIGHT = 0.1  # m2 of gain one more metre of travel is worth
MERGE_M = 0.3  # viewpoints this close are one
TOUR_BIAS_M = 1.0  # gain per (travel + this) picks which pieces enter the tour
# While driving to a viewpoint (ros_ops navigate --watch-frontier): once less than
# MIN_FRONTIER_M of frontier is left within this distance of its piece's centre, the
# piece has been seen on the way and the next viewpoint is chosen.
WATCH_RADIUS_M = 0.7
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
    gain_m2: float = 0.0  # unknown area a scan from here would newly see through the piece
    fx: float = 0.0  # the frontier piece's centre (ros_ops navigate --watch-frontier)
    fy: float = 0.0

    def to_json(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in self.__dict__.items()}


def wall_distance(grid: Grid, max_cells: int) -> list[int]:
    """Cells (8-neighbour steps) to the nearest occupied cell, capped at max_cells."""
    return distance_to(grid, lambda v: v >= OCCUPIED_MIN, max_cells)


def distance_to(grid: Grid, source: Callable[[int], bool], max_cells: int) -> list[int]:
    """Cells (8-neighbour steps) to the nearest cell whose value matches, capped."""
    far = max_cells
    distance = [far] * (grid.width * grid.height)
    queue: deque[tuple[int, int]] = deque()
    for row in range(grid.height):
        for col in range(grid.width):
            if source(grid.value(col, row)):
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


def segments(cluster: list[tuple[int, int]], tile: int) -> list[list[tuple[int, int]]]:
    """A cluster split by tile x tile cell squares (a short cluster stays whole)."""
    pieces: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for col, row in cluster:
        pieces.setdefault((col // tile, row // tile), []).append((col, row))
    return list(pieces.values())


def candidates(
    grid: Grid,
    robot: tuple[float, float],
    *,
    avoid: Iterable[tuple[float, float]] = (),
    avoid_radius_m: float = 0.3,
    limit: int = 10,
) -> list[Target]:
    """Viewpoints worth a leg, in the order of the shortest tour from the robot."""
    start = grid.cell(*robot)
    if start is None:
        return []
    width, res = grid.width, grid.resolution
    clearance = math.ceil(CLEARANCE_M / res)
    radius = math.ceil(ROBOT_RADIUS_M / res)
    walls = wall_distance(grid, clearance + 1)
    # The whole footprint on known space, so the planner accepts the goal.
    unknown = distance_to(grid, lambda v: v < 0, radius + 1)
    passable = [
        grid.free(i % width, i // width) and walls[i] >= radius for i in range(width * grid.height)
    ]
    passable[start[1] * width + start[0]] = True  # the car may stand near a wall
    cost = reachable(grid, start, passable)
    avoided = list(avoid)
    search = math.ceil(SEARCH_RADIUS_M / res)
    tile = max(1, round(SEGMENT_M / res))
    rays = ray_offsets(res)
    found = []
    for piece in (p for c in frontier_clusters(grid) for p in segments(c, tile)):
        length = len(piece) * res
        if length < MIN_FRONTIER_M:
            continue
        cx = sum(c for c, _ in piece) / len(piece)
        cy = sum(r for _, r in piece) / len(piece)
        through = {
            (r + dr) * width + c + dc for c, r in piece for dc, dr in NEIGHBOURS_8 + ((0, 0),)
        }
        # Sample viewpoints: the cell nearest the piece in each distance band and direction.
        samples: dict[tuple[int, int], tuple[float, int]] = {}
        for row in range(max(0, int(cy) - search), min(grid.height, int(cy) + search + 1)):
            for col in range(max(0, int(cx) - search), min(width, int(cx) + search + 1)):
                i = row * width + col
                if i not in cost or walls[i] < clearance or unknown[i] < radius:
                    continue
                gap = math.hypot(col - cx, row - cy)
                if gap > search:
                    continue
                band = int(gap * res / VIEWPOINT_BAND_M)
                turn = (math.atan2(row - cy, col - cx) + math.pi) / (2 * math.pi)
                key = (band, int(turn * VIEWPOINT_DIRECTIONS) % VIEWPOINT_DIRECTIONS)
                if key not in samples or gap < samples[key][0]:
                    samples[key] = (gap, i)
        best: tuple[float, float, int] | None = None  # score, gain, cell
        for _, i in samples.values():
            col, row = i % width, i // width
            x, y = grid.centre(col, row)
            if cost[i] < MIN_TRAVEL_M:
                continue  # where the car already stands
            if any(math.hypot(x - ax, y - ay) < avoid_radius_m for ax, ay in avoided):
                continue
            gain = visible_unknown(grid, col, row, rays, through) * res * res
            score = gain - VIEWPOINT_TRAVEL_WEIGHT * cost[i]
            if best is None or score > best[0]:
                best = (score, gain, i)
        if best is None or best[1] < MIN_GAIN_M2:
            continue
        _, gain, i = best
        x, y = grid.centre(i % width, i // width)
        fx, fy = grid.centre(round(cx), round(cy))
        if math.hypot(fx - x, fy - y) >= FACE_MIN_M:
            yaw = math.atan2(fy - y, fx - x)
        else:  # too close to tell: keep the heading the car arrives with
            yaw = math.atan2(y - robot[1], x - robot[0])
        found.append(Target(x, y, yaw, length, cost[i], gain, fx, fy))
    # One viewpoint per spot, then the most gain per metre fill the tour.
    merged: list[Target] = []
    for target in sorted(found, key=lambda t: -t.gain_m2):
        if all(math.hypot(target.x - m.x, target.y - m.y) >= MERGE_M for m in merged):
            merged.append(target)
    chosen = sorted(merged, key=lambda t: -t.gain_m2 / (t.travel_m + TOUR_BIAS_M))[:limit]
    cells = [grid.cell(t.x, t.y) or start for t in chosen]
    between = []
    for col, row in cells:
        reach = reachable(grid, (col, row), passable)
        between.append([reach.get(r * width + c, math.inf) for c, r in cells])
    return [chosen[i] for i in tour([t.travel_m for t in chosen], between)]


def ray_offsets(resolution: float) -> list[list[tuple[int, int]]]:
    """Cells each of GAIN_RAYS rays crosses from a cell's centre out to SENSOR_RANGE_M.

    Quarter-cell steps, so a ray does not slip between two diagonal wall cells.
    """
    steps = math.ceil(4 * SENSOR_RANGE_M / resolution)
    rays = []
    for k in range(GAIN_RAYS):
        angle = 2 * math.pi * k / GAIN_RAYS
        dx, dy = math.cos(angle) / 4, math.sin(angle) / 4
        path: list[tuple[int, int]] = []
        for s in range(1, steps + 1):
            cell = (math.floor(0.5 + s * dx), math.floor(0.5 + s * dy))
            if cell != (0, 0) and (not path or path[-1] != cell):
                path.append(cell)
        rays.append(path)
    return rays


def visible_unknown(
    grid: Grid,
    col: int,
    row: int,
    rays: Sequence[Sequence[tuple[int, int]]],
    through: set[int] | None = None,
) -> int:
    """Unknown cells a scan from (col, row) would cross before a wall; beyond the map too.

    With `through` (a frontier piece and its neighbours, as cell indices) a ray counts
    only the unknown it enters from one of those cells, up to where it comes out on
    known space again: what that piece opens up, not what other openings would show.
    """
    seen: set[tuple[int, int]] = set()
    width, height, data = grid.width, grid.height, grid.data
    for path in rays:
        last_known = row * width + col
        in_unknown = False
        for dc, dr in path:
            c, r = col + dc, row + dr
            if 0 <= c < width and 0 <= r < height:
                i = r * width + c
                value = data[i]
                if value >= OCCUPIED_MIN:
                    break
                if value >= 0:
                    if in_unknown and through is not None:
                        break  # out on known space again: another frontier's business
                    last_known, in_unknown = i, False
                    continue
            if not in_unknown and through is not None and last_known not in through:
                break  # this ray leaves known space through some other opening
            in_unknown = True
            seen.add((c, r))
    return len(seen)


def frontier_left(grid: Grid, x: float, y: float, radius_m: float = WATCH_RADIUS_M) -> float:
    """Metres of frontier (free cells beside unknown) within radius_m of (x, y)."""
    centre = grid.cell(x, y)
    if centre is None:
        return 0.0
    reach = math.ceil(radius_m / grid.resolution)
    cells = 0
    for row in range(max(0, centre[1] - reach), min(grid.height, centre[1] + reach + 1)):
        for col in range(max(0, centre[0] - reach), min(grid.width, centre[0] + reach + 1)):
            if math.hypot(col - centre[0], row - centre[1]) > reach or not grid.free(col, row):
                continue
            if any(
                grid.inside(col + dc, row + dr) and grid.value(col + dc, row + dr) < 0
                for dc, dr in NEIGHBOURS_4
            ):
                cells += 1
    return cells * grid.resolution


def seen_on_the_way(grid: Grid, fx: float, fy: float) -> bool:
    """The goal's frontier piece is gone (mapped while driving): no need to arrive."""
    return frontier_left(grid, fx, fy) < MIN_FRONTIER_M


def tour(start: Sequence[float], between: Sequence[Sequence[float]]) -> list[int]:
    """Visiting order of the shortest open path from the robot through every viewpoint.

    start[i] is the travel from the robot to viewpoint i, between[i][j] from i to j.
    Exact (Held-Karp, n * 2^n states); candidates() hands it at most `limit` (10).
    """
    n = len(start)
    if n == 0:
        return []
    full = (1 << n) - 1
    best = [[math.inf] * n for _ in range(full + 1)]
    parent = [[-1] * n for _ in range(full + 1)]
    for j in range(n):
        best[1 << j][j] = start[j]
    for mask in range(1, full + 1):
        for j in range(n):
            here = best[mask][j]
            if here == math.inf:
                continue
            for k in range(n):
                if mask & (1 << k):
                    continue
                value = here + between[j][k]
                if value < best[mask | (1 << k)][k]:
                    best[mask | (1 << k)][k] = value
                    parent[mask | (1 << k)][k] = j
    last = min(range(n), key=lambda j: best[full][j])
    if best[full][last] == math.inf:  # some viewpoints cannot reach each other
        return sorted(range(n), key=lambda j: start[j])
    order, mask = [], full
    while last != -1:
        order.append(last)
        last, mask = parent[mask][last], mask & ~(1 << last)
    return order[::-1]


def explored_m2(grid: Grid) -> float:
    free = sum(1 for v in grid.data if 0 <= v <= FREE_MAX)
    return free * grid.resolution**2
