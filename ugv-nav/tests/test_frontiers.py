"""Frontier targets: reachable, clear of walls, facing the unknown, nearest first."""

import itertools
import math
import random

import pytest

from ugv_nav_tools.frontiers import (
    CLEARANCE_M,
    MIN_GAIN_M2,
    Grid,
    candidates,
    explored_m2,
    frontier_clusters,
    frontier_left,
    ray_offsets,
    reachable,
    seen_on_the_way,
    segments,
    tour,
    visible_unknown,
    wall_distance,
)

RES = 0.05


def grid_from(rows: list[str]) -> Grid:
    """ASCII map, top row first: '#' wall, '.' free, '?' unknown."""
    values = {"#": 100, ".": 0, "?": -1}
    height, width = len(rows), len(rows[0])
    data = [values[ch] for row in reversed(rows) for ch in row]
    return Grid(width, height, RES, 0.0, 0.0, data)


def room(width: int = 40, height: int = 30, door: tuple[int, int] | None = (12, 18)) -> Grid:
    """A walled room; beyond its right wall (through an optional doorway) lies unknown."""
    rows = []
    for r in range(height):
        line = ""
        for c in range(width + 12):
            if c > width:
                line += "?"
            elif r in (0, height - 1) or c in (0, width):
                is_door = door is not None and c == width and door[0] <= r < door[1]
                line += "." if is_door else "#"
            else:
                line += "."
        rows.append(line)
    return grid_from(rows)


def test_cells_and_wall_distance() -> None:
    grid = grid_from(["###", "#.#", "###"])
    assert grid.cell(0.06, 0.06) == (1, 1) and grid.cell(-0.01, 0) is None
    assert grid.centre(1, 1) == pytest.approx((0.075, 0.075))
    assert wall_distance(grid, 5)[1 * 3 + 1] == 1
    far = wall_distance(grid_from(["....."]), 3)
    assert far == [3] * 5  # no walls: capped


def test_reachability_goes_around_walls() -> None:
    grid = grid_from(["....", ".##.", "...."])
    cost = reachable(grid, (0, 0), [grid.free(i % 4, i // 4) for i in range(12)])
    assert cost[2 * 4 + 0] == pytest.approx(2 * RES)  # straight up the left side
    assert (1 * 4 + 1) not in cost  # walls are never reached


def test_frontiers_need_unknown_neighbours() -> None:
    assert frontier_clusters(grid_from(["...", "..."])) == []
    clusters = frontier_clusters(grid_from(["..?", "..?", "###"]))
    assert len(clusters) == 1 and sorted(clusters[0]) == [(1, 1), (1, 2)]


def test_doorway_target_is_clear_reachable_and_faces_the_unknown() -> None:
    grid = room()
    targets = candidates(grid, robot=(0.5, 0.75))
    assert targets, "the open doorway should give a frontier"
    first = targets[0]
    col, row = grid.cell(first.x, first.y) or (0, 0)
    assert wall_distance(grid, 20)[row * grid.width + col] >= math.ceil(CLEARANCE_M / RES)
    assert first.travel_m >= 0.30 and first.frontier_m >= 0.30
    assert abs(first.yaw) < math.pi / 2  # the unknown is to the right (east)


def test_closed_room_and_unreachable_frontiers_give_nothing() -> None:
    assert candidates(room(door=None), robot=(0.5, 0.75)) == []
    assert candidates(room(), robot=(-1.0, 0.0)) == []  # robot outside the map


def test_failed_targets_are_avoided_and_explored_area_counts_free_cells() -> None:
    grid = room()
    first = candidates(grid, robot=(0.5, 0.75))[0]
    again = candidates(grid, robot=(0.5, 0.75), avoid=[(first.x, first.y)])
    assert all(math.hypot(t.x - first.x, t.y - first.y) >= 0.3 for t in again)
    assert explored_m2(grid_from(["..#?"])) == pytest.approx(2 * RES * RES)
    assert first.to_json()["x"] == round(first.x, 3)


def test_a_frontier_ringing_the_car_gives_targets_all_around() -> None:
    size, ring = 80, 30  # 4 m square, known free out to 1.5 m from the centre
    rows = []
    for r in range(size):
        line = ""
        for c in range(size):
            line += "." if math.hypot(c - size / 2, r - size / 2) < ring else "?"
        rows.append(line)
    grid = grid_from(rows)
    centre = size / 2 * RES
    targets = candidates(grid, robot=(centre, centre))
    assert len(targets) >= 4
    for target in targets:  # each faces outwards, towards its piece of the frontier
        outward = math.atan2(target.y - centre, target.x - centre)
        assert abs(math.atan2(math.sin(target.yaw - outward), math.cos(target.yaw - outward))) < 1.0
        assert math.hypot(target.x - centre, target.y - centre) > 0.3


def test_segments_cut_long_frontiers_by_tile() -> None:
    line = [(c, 0) for c in range(45)]
    assert [len(p) for p in segments(line, 20)] == [20, 20, 5]
    assert segments([(1, 1), (2, 1)], 20) == [[(1, 1), (2, 1)]]


def test_gain_counts_unknown_a_scan_would_reach() -> None:
    rays = ray_offsets(RES)
    assert visible_unknown(room(door=None), 20, 15, rays) == 0  # walls all round
    open_door = room()
    near, far = visible_unknown(open_door, 32, 15, rays), visible_unknown(open_door, 8, 15, rays)
    assert near > far > 0  # the doorway's view widens as the car comes close
    door = {r * open_door.width + 40 for r in range(11, 19)}
    assert visible_unknown(open_door, 32, 15, rays, through=door) == near
    assert visible_unknown(open_door, 32, 15, rays, through={0}) == 0  # not through here


def test_a_frontier_onto_a_small_pocket_is_not_worth_a_leg() -> None:
    rows = []
    for r in range(30):
        line = ""
        for c in range(52):
            if c > 40:
                line += "?"
            elif r in (0, 29) or c in (0, 40):
                line += "." if c == 40 and 12 <= r < 18 else "#"
            elif 12 <= r < 18 and 8 <= c < 12:
                line += "?"  # 20 x 30 cm under a cupboard: 0.06 m2
            else:
                line += "."
        rows.append(line)
    targets = candidates(grid_from(rows), robot=(1.0, 0.75))
    assert targets and all(t.x > 1.2 for t in targets)  # only towards the doorway
    assert all(t.gain_m2 >= MIN_GAIN_M2 for t in targets)


def corridor() -> Grid:
    """8 m x 1.2 m corridor, unknown past both ends and above a 45 cm opening at x 2.5 m."""
    rows = []
    for r in range(41):
        line = ""
        for c in range(161):
            if c < 4 or c > 156 or r > 25:
                line += "?"
            elif r == 0 or (r == 25 and not 46 <= c <= 54):
                line += "#"
            else:
                line += "."
        rows.append(line)
    return grid_from(list(reversed(rows)))  # row 0 of the list is the top


def test_the_tour_not_the_nearest_target_comes_first() -> None:
    targets = candidates(corridor(), robot=(2.0, 0.6))
    assert len(targets) == 3
    nearest = min(targets, key=lambda t: t.travel_m)
    assert 2.2 < nearest.x < 2.8  # the side opening is closest ...
    assert targets[0].x < 1.0  # ... but the left end first saves walking back past it
    assert targets[-1].x > 7.0


def path_length(
    order: tuple[int, ...] | list[int], start: list[float], between: list[list[float]]
) -> float:
    return start[order[0]] + sum(between[a][b] for a, b in zip(order, order[1:], strict=False))


def test_tour_is_exact() -> None:
    # On a line: start at 0, stops at +1, -1.5, +3, +4. Nearest-first walks 9 m, the
    # tour 7 m (left first).
    xs = [1.0, -1.5, 3.0, 4.0]
    between = [[abs(a - b) for b in xs] for a in xs]
    assert tour([abs(x) for x in xs], between) == [1, 0, 2, 3]
    rng = random.Random(7)
    for n in range(1, 7):
        points = [(rng.uniform(-3, 3), rng.uniform(-3, 3)) for _ in range(n)]
        start = [math.hypot(*p) for p in points]
        between = [[math.dist(p, q) for q in points] for p in points]
        best = min(path_length(p, start, between) for p in itertools.permutations(range(n)))
        assert path_length(tour(start, between), start, between) == pytest.approx(best)
    assert tour([], []) == []
    assert tour([1.0, 2.0], [[0, math.inf], [math.inf, 0]]) == [0, 1]  # cut off: by distance


def test_a_frontier_mapped_on_the_way_ends_the_leg() -> None:
    open_door = room()
    assert frontier_left(open_door, 2.0, 0.75) == pytest.approx(6 * RES)  # the doorway
    assert not seen_on_the_way(open_door, 2.0, 0.75)
    assert seen_on_the_way(room(door=None), 2.0, 0.75)  # nothing left there
    assert seen_on_the_way(open_door, -3.0, 0.0)  # off the map
