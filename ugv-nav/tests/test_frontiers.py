"""Frontier targets: reachable, clear of walls, facing the unknown, nearest first."""

import math

import pytest

from ugv_nav_tools.frontiers import (
    Grid,
    candidates,
    explored_m2,
    frontier_clusters,
    reachable,
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
    assert wall_distance(grid, 20)[row * grid.width + col] >= math.ceil(0.40 / RES)
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
