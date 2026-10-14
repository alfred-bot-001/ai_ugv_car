"""Scan statistics used for N0 acceptance must be right before we trust a LiDAR report."""

import math
from types import SimpleNamespace

import pytest

from ugv_nav_tools.scan_report import report
from ugv_nav_tools.scan_summary import Sector, is_valid, occluded_arcs, rate_hz, sectors


def test_validity_and_rate() -> None:
    assert is_valid(1.2)
    assert not any(map(is_valid, (0.0, -1.0, math.nan, math.inf)))
    assert rate_hz([]) is None
    assert rate_hz([5.0, 5.0]) is None
    assert rate_hz([0.0, 0.1, 0.2, 0.3]) == pytest.approx(10.0)


def test_sectors_wrap_negative_angles_and_count_body_returns() -> None:
    # Four bins at -10, 80, 170, 260 degrees.
    found = sectors([0.1, 2.0, math.nan, 0.0], math.radians(-10), math.radians(90), 90)
    assert [s.start_deg for s in found] == [0, 90, 180, 270]
    by_start = {s.start_deg: s for s in found}
    assert by_start[270].near_body_share == 1.0  # -10 deg lands in [270, 360)
    assert by_start[0].nearest_m == 2.0
    assert by_start[90].invalid_share == 1.0 and by_start[90].nearest_m is None
    with pytest.raises(ValueError):
        sectors([1.0], 0, 1, 7)


def test_occluded_arcs_merge_and_wrap() -> None:
    def sector(start: int, blocked: bool) -> Sector:
        return Sector(start, 1.0, 1.0 if blocked else 0.0, 0.0)

    blocked = {0, 90, 180, 270}
    found = [sector(start, start in blocked) for start in range(0, 360, 90)]
    assert occluded_arcs(found) == [(0, 360)]
    found = [sector(s, s in {0, 270}) for s in range(0, 360, 90)]
    assert occluded_arcs(found) == [(270, 450)]
    found = [sector(s, s == 90) for s in range(0, 360, 90)]
    assert occluded_arcs(found) == [(90, 180)]
    assert occluded_arcs(found[:1]) == []


def test_report_without_and_with_scan() -> None:
    assert report([], None, 15, 0.3) == {"messages": 0, "rate_hz": None}
    scan = SimpleNamespace(
        header=SimpleNamespace(frame_id="base_lidar_link"),
        ranges=[1.0] * 36,
        angle_min=0.0,
        angle_max=math.radians(350),
        angle_increment=math.radians(10),
    )
    result = report([0.0, 0.1], scan, 90, 0.3)
    assert result["rate_hz"] == pytest.approx(10.0)
    assert result["valid_bins"] == 36 and result["occluded_arcs_deg"] == []
    assert len(result["sectors"]) == 4
