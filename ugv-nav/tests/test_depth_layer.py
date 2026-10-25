"""The front depth scan from pi-agent: strict checks, then LaserScan ranges."""

import math
from pathlib import Path
from typing import Any

import pytest

from ugv_nav_tools.board_client import BoardClient
from ugv_nav_tools.depth_layer import parse_depth_scan

SCAN = {
    "sequence": 12,
    "age": 0.21,
    "angle_min": -0.602139,
    "angle_increment": 0.017453,
    "range_max": 2.0,
    "ranges_mm": [0, None, 930, 1499, 0],
}


def test_ranges_become_metres_clear_is_inf_unseen_is_nan() -> None:
    scan = parse_depth_scan(SCAN)
    assert scan is not None and scan.sequence == 12 and scan.age == 0.21
    assert scan.ranges[0] == math.inf and math.isnan(scan.ranges[1])
    assert scan.ranges[2:4] == [0.93, 1.499] and scan.ranges[4] == math.inf
    assert scan.angle_max == pytest.approx(-0.602139 + 4 * 0.017453)
    assert parse_depth_scan(None) is None


@pytest.mark.parametrize(
    "change",
    [
        {"sequence": "12"},
        {"sequence": True},
        {"age": 5.0},
        {"age": None},
        {"angle_min": 0.5},
        {"angle_increment": 0.0},
        {"range_max": math.inf},
        {"ranges_mm": []},
        {"ranges_mm": [0] * 361},
        {"ranges_mm": [2500]},  # beyond range_max
        {"ranges_mm": [-5]},
        {"ranges_mm": [False]},
        {"ranges_mm": ["930"]},
        {"angle_min": -0.1, "angle_increment": 0.2, "ranges_mm": [0] * 20},  # past half a turn
    ],
)
def test_malformed_scans_are_refused(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_depth_scan({**SCAN, **change})
    with pytest.raises(ValueError):
        parse_depth_scan([SCAN])


def test_board_client_asks_for_depth_only_when_told(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []
    client = BoardClient(Path("/nonexistent.sock"))
    monkeypatch.setattr(client, "request", lambda payload: sent.append(payload) or {"ok": True})
    client.status()
    client.status(depth=True)
    assert sent == [{"op": "status"}, {"op": "status", "depth": True}]
