"""Localization health checks (S0 §3.2), tested without ROS."""

import math

from ugv_nav_tools.loc_health import (
    MATCH_SECONDS,
    OCCUPIED,
    JumpWatch,
    LocalizationHealth,
    MatchWatch,
    Stamped,
    covariance_fault,
    match_ratio,
    scan_endpoints,
)
from ugv_nav_tools.stall import Pose2D


def at(t: float, x: float = 0.0, y: float = 0.0, yaw: float = 0.0) -> Stamped:
    return Stamped(t, Pose2D(x, y, yaw))


def test_smooth_corrections_pass_and_a_jump_is_held_for_a_second() -> None:
    watch = JumpWatch()
    for i in range(10):  # 2 cm per 0.2 s: a normal correction
        assert watch.add(at(i / 5, x=0.02 * i)) is None
    assert watch.add(at(2.0, x=0.9)) is not None  # the 2026-10-14 sized jump
    assert watch.add(at(2.0, x=0.9)) is not None  # duplicate stamp keeps the verdict
    assert watch.add(at(2.6, x=0.9)) is not None  # pre-jump samples still in the window
    assert watch.add(at(3.2, x=0.9)) is None  # a quiet second has passed
    assert watch.add(at(3.4, x=0.9, yaw=0.5)) is not None  # rotation jump
    watch.reset()
    assert watch.add(at(10.0)) is None


def test_scan_endpoints_use_the_sensor_pose_and_drop_invalid_returns() -> None:
    sensor = Pose2D(1.0, 2.0, math.pi / 2)
    points = scan_endpoints([1.0, math.inf, 0.01, 2.0], 0.0, math.pi / 2, sensor, 0.05, 12.0)
    assert len(points) == 2
    assert points[0] == (1.0 + math.cos(math.pi / 2), 2.0 + 1.0)
    # index 3 sits at pi/2 + 3 * pi/2 = 2 pi: straight along +x in the map
    assert abs(points[1][0] - 3.0) < 1e-9 and abs(points[1][1] - 2.0) < 1e-9


def test_match_ratio_counts_hits_within_one_cell() -> None:
    width, height = 10, 10
    data = [0] * (width * height)
    data[5 * width + 5] = OCCUPIED  # wall cell (5, 5) at 0.1 m resolution
    near = (0.64, 0.52)  # cell (6, 5): one cell away
    far = (0.95, 0.95)  # cell (9, 9)
    outside = (5.0, 5.0)
    assert match_ratio([near, far, outside], data, width, height, 0.1, 0.0, 0.0) == 1 / 3
    assert match_ratio([], data, width, height, 0.1, 0.0, 0.0) is None
    assert match_ratio([near], data, width, height, 0.1, 0.0, 0.0, tolerance=0) == 0.0


def test_match_watch_needs_a_sustained_low_match() -> None:
    watch = MatchWatch()
    assert watch.add(0.0, 0.3) is None
    assert watch.add(MATCH_SECONDS - 0.1, 0.3) is None
    assert watch.add(MATCH_SECONDS, 0.3) is not None
    assert watch.add(MATCH_SECONDS + 0.2, 0.8) is None  # recovered
    assert watch.add(10.0, None) is None  # no scan or map: nothing to judge
    watch.reset()
    assert watch.low_since is None


def test_covariance_limits() -> None:
    narrow = [0.0] * 36
    narrow[0] = narrow[7] = 0.01  # 0.1 m
    narrow[35] = 0.01
    assert covariance_fault(narrow) is None
    wide = list(narrow)
    wide[7] = 0.16  # 0.4 m
    assert covariance_fault(wide) is not None
    turned = list(narrow)
    turned[35] = 0.25  # 0.5 rad
    assert covariance_fault(turned) is not None


def test_mapping_mode_judges_only_jumps() -> None:
    wide = [0.25] * 36
    mapping = LocalizationHealth(localization=False)
    mapping.on_covariance(wide)
    mapping.on_match(0.0, 0.0)
    mapping.on_match(5.0, 0.0)
    assert mapping.reason is None
    mapping.on_transform(at(0.0))
    mapping.on_transform(at(0.2, x=0.5))
    assert mapping.reason is not None and "jumped" in mapping.reason

    located = LocalizationHealth(localization=True)
    located.on_covariance(wide)
    assert located.reason is not None and "AMCL" in located.reason
