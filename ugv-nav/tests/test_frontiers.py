"""Frontier targets: reachable, clear of walls, facing the unknown, nearest first."""

import math

import pytest

from ugv_nav_tools.frontiers import (
    CLEARANCE_M,
    Grid,
    candidates,
    explored_m2,
    frontier_clusters,
    reachable,
    segments,
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
