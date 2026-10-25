"""Localization health checks (S0 §3.2), tested without ROS."""

import math

from ugv_nav_tools.loc_health import (
    JUMP_ROTATION_RAD,
    JUMP_TRANSLATION_M,
    MAPPING_JUMP_ROTATION_RAD,
    MAPPING_JUMP_TRANSLATION_M,
    MATCH_SECONDS,
    OCCUPIED,
    JumpWatch,
    LocalizationHealth,
    MatchWatch,
    Stamped,
    covariance_fault,
    match_ratio,
    scan_endpoints,
    should_halt,
)
from ugv_nav_tools.stall import Pose2D


def at(t: float, x: float = 0.0, y: float = 0.0, yaw: float = 0.0) -> Stamped:
    return Stamped(t, Pose2D(x, y, yaw))


ROBOT = (4.0, 0.0)  # robot 4 m from the odom origin, as late in a mapping run


def heading_fix(t: float, yaw: float, robot: tuple[float, float] = ROBOT) -> Stamped:
    """map->odom that turns the robot in place by `yaw`: the odom origin swings r*yaw."""
    rx, ry = robot
    tx = rx - (rx * math.cos(yaw) - ry * math.sin(yaw))
    ty = ry - (rx * math.sin(yaw) + ry * math.cos(yaw))
    return Stamped(t, Pose2D(tx, ty, yaw), robot)


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
    mapping.on_transform(at(0.2, x=0.9))  # the 2026-10-14 sized jump
    assert mapping.reason is not None and "jumped" in mapping.reason

    located = LocalizationHealth(localization=True)
    located.on_covariance(wide)
    assert located.reason is not None and "AMCL" in located.reason


def test_halt_only_while_navigation_drives() -> None:
    driving = {"nav_owned": True, "fault": None}
    assert should_halt("map->odom jumped 0.30 m", driving)
    assert not should_halt(None, driving)
    assert not should_halt("map->odom jumped 0.30 m", {"nav_owned": False, "fault": None})
    assert not should_halt("jump", {"nav_owned": True, "fault": {"reason": "stall_body"}})
    assert not should_halt("jump", {})


def test_jump_is_measured_at_the_robot_not_at_the_odom_origin() -> None:
    # 2026-10-25: SLAM turned the heading 0.3 rad with the car 4 m out; the odom
    # origin swung 1.2 m while the car itself did not move on the map.
    watch = JumpWatch()
    assert watch.add(heading_fix(0.0, 0.0)) is None
    assert watch.add(heading_fix(0.2, 0.3)) is None
    # A real 0.9 m shift of the car on the map is still a jump wherever the origin is.
    assert watch.add(Stamped(0.4, Pose2D(0.9, 0.0, 0.0), ROBOT)) is not None


def test_without_a_robot_position_the_origin_stands_in() -> None:
    watch = JumpWatch()
    assert watch.add(at(0.0)) is None
    assert watch.add(at(0.2, x=0.3)) is not None


def test_mapping_mode_has_its_own_jump_thresholds() -> None:
    assert MAPPING_JUMP_TRANSLATION_M > JUMP_TRANSLATION_M
    assert MAPPING_JUMP_ROTATION_RAD > JUMP_ROTATION_RAD
    # A 0.42 rad SLAM heading fix (seen at every mapping start on 2026-10-25) is normal
    # while mapping and a fault while localizing on a saved map.
    mapping = LocalizationHealth(localization=False)
    mapping.on_transform(heading_fix(0.0, 0.0))
    mapping.on_transform(heading_fix(0.2, 0.42))
    assert mapping.reason is None
    located = LocalizationHealth(localization=True)
    located.on_transform(heading_fix(0.0, 0.0))
    located.on_transform(heading_fix(0.2, 0.42))
    assert located.reason is not None and "jumped" in located.reason
    # Translation: 0.4 m at the robot passes while mapping, fails while localizing.
    mapping.on_transform(Stamped(2.0, Pose2D(0.0, 0.0, 0.0), ROBOT))
    mapping.on_transform(Stamped(2.2, Pose2D(0.4, 0.0, 0.0), ROBOT))
    assert mapping.reason is None
    mapping.on_transform(Stamped(2.4, Pose2D(1.1, 0.0, 0.0), ROBOT))  # 0.7 m / 0.2 s
    assert mapping.reason is not None
    located.on_transform(Stamped(2.0, Pose2D(0.0, 0.0, 0.0), ROBOT))
    located.on_transform(Stamped(2.2, Pose2D(0.4, 0.0, 0.0), ROBOT))
    assert located.reason is not None
