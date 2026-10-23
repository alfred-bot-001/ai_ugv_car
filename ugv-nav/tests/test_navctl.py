"""Operator CLI: the arm window must always close, and routes are validated up front."""

import json
import math
import os
import stat
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from ugv_nav_tools.navctl import (
    BatteryLow,
    NavCtl,
    Settings,
    main,
    map_name,
    mode_env,
    write_arm,
)
from ugv_nav_tools.route import (
    Pose,
    goal_errors,
    load_route,
    parse_route,
    quaternion_to_yaw,
    within_tolerance,
    yaw_to_quaternion,
)


def test_pose_math_and_tolerance() -> None:
    z, w = yaw_to_quaternion(math.pi / 2)
    assert quaternion_to_yaw(z, w) == pytest.approx(math.pi / 2)
    assert goal_errors(Pose(0, 0, math.pi - 0.05), Pose(0, 0.03, -math.pi + 0.05)) == (
        pytest.approx(0.03),
        pytest.approx(0.1),
    )
    assert within_tolerance(Pose(0.05, 0, 0.1), Pose(0, 0, 0))
    assert within_tolerance(Pose(0.08, 0, 0), Pose(0, 0, 0))
    assert not within_tolerance(Pose(0.11, 0, 0), Pose(0, 0, 0))
    assert within_tolerance(Pose(0, 0, 0.34), Pose(0, 0, 0))
    assert not within_tolerance(Pose(0, 0, 0.36), Pose(0, 0, 0))
    with pytest.raises(ValueError):
        Pose(math.nan, 0, 0)


def test_route_validation(tmp_path: Path) -> None:
    route = parse_route({"loops": 2, "waypoints": [{"x": 1, "y": 0}, {"x": 0, "y": 0, "yaw": 3}]})
    assert [(loop, index) for loop, index, _ in route.legs()] == [(1, 1), (1, 2), (2, 1), (2, 2)]
    assert parse_route({"waypoints": [{"x": 1, "y": 0, "name": "门口"}]}, loops=3).loops == 3
    for bad in (
        [],
        {"waypoints": []},
        {"waypoints": [{"x": 1}]},
        {"waypoints": [{"x": 1, "y": 0, "speed": 9}]},
        {"waypoints": [{"x": "a", "y": 0}]},
        {"waypoints": [{"x": 1, "y": 0}], "loops": 0},
        {"waypoints": [{"x": 1, "y": 0}] * 21},
    ):
        with pytest.raises(ValueError):
            parse_route(bad)
    path = tmp_path / "route.json"
    path.write_text(json.dumps({"waypoints": [{"x": 0.2, "y": 0.1}]}))
    assert load_route(path).waypoints == (Pose(0.2, 0.1, 0.0),)


def test_map_names_and_mode_env() -> None:
    assert map_name("living-room_1") == "living-room_1"
    for bad in ("", "../x", "a b", "x" * 65):
        with pytest.raises(ValueError):
            map_name(bad)
    assert "mapping:=true localization:=false navigation:=true" in mode_env(mapping=True)
    assert "map:=/maps/room.yaml" in mode_env(mapping=False, map_file="/maps/room.yaml")


def test_write_arm_is_private_and_bounded(tmp_path: Path) -> None:
    session = write_arm(tmp_path, enabled=True, now=1000.0)
    value = json.loads((tmp_path / "arm.json").read_text())
    assert value["enabled"] and value["id"] == session and value["expires_at"] == 1120.0
    assert stat.S_IMODE(os.stat(tmp_path / "arm.json").st_mode) == 0o600
    with pytest.raises(ValueError):
        write_arm(tmp_path, enabled=True, now=0, seconds=121)
    assert write_arm(tmp_path, enabled=False, now=0) == ""
    assert json.loads((tmp_path / "arm.json").read_text()) == {"enabled": False}


class FakeRos:
    """Records docker exec calls and answers ugv-ros-op with canned JSON."""

    def __init__(self, navigate: list[dict[str, Any]], board: Path) -> None:
        self.navigate = navigate
        self.board = board
        self.calls: list[list[str]] = []
        self.armed_during_navigate: list[bool] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        stdout = ""
        if "ugv-ros-op" in command:
            op = command[command.index("ugv-ros-op") + 1]
            if op == "navigate":
                arm = json.loads((self.board / "arm.json").read_text())
                self.armed_during_navigate.append(arm["enabled"])
                stdout = json.dumps(self.navigate.pop(0))
            else:
                stdout = json.dumps({"op": op})
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def make_ctl(tmp_path: Path, navigate: list[dict[str, Any]]) -> tuple[NavCtl, FakeRos, Mock]:
    settings = Settings(
        deploy=tmp_path / "deploy", board=tmp_path / "board", data=tmp_path / "data"
    )
    settings.board.mkdir()
    (settings.compose_dir).mkdir(parents=True)
    settings.maps.mkdir(parents=True)
    fake = FakeRos(navigate, settings.board)
    board = Mock()
    board.status.return_value = {
        "ok": True,
        "age": 0.1,
        "estopped": False,
        "nav_owned": False,
        "telemetry": {"L": 0.0, "R": 0.0, "v": 12.0},
    }
    ctl = NavCtl(settings, run=fake, board=board, echo=lambda _: None, sleep=lambda _: None)
    return ctl, fake, board


def test_goal_arms_only_while_navigating_and_always_disarms(tmp_path: Path) -> None:
    ctl, fake, board = make_ctl(tmp_path, [{"ok": True}, {"ok": False}])
    assert ctl.goal(Pose(0.2, 0, 0))["ok"]
    assert fake.armed_during_navigate == [True]
    assert json.loads((ctl.settings.board / "arm.json").read_text()) == {"enabled": False}
    assert board.stop.called
    assert fake.calls[0][:5] == ["sudo", "-n", "docker", "exec", "ugv-nav"]

    def explode(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "navigate" in command:
            raise KeyboardInterrupt
        return fake(command, **kwargs)

    ctl.run = explode
    with pytest.raises(KeyboardInterrupt):
        ctl.goal(Pose(0.2, 0, 0))
    assert json.loads((ctl.settings.board / "arm.json").read_text()) == {"enabled": False}


def test_patrol_stops_at_first_failed_leg_and_writes_report(tmp_path: Path) -> None:
    ctl, fake, _ = make_ctl(tmp_path, [{"ok": True}, {"ok": False, "error": "blocked"}])
    route = parse_route({"loops": 2, "waypoints": [{"x": 1, "y": 0}, {"x": 0, "y": 0}]})
    summary = ctl.patrol(route)
    assert not summary["ok"] and summary["completed_legs"] == 1 and summary["planned_legs"] == 4
    assert [leg["index"] for leg in summary["legs"]] == [1, 2]
    reports = list(ctl.settings.reports.glob("patrol-*.json"))
    assert len(reports) == 1 and json.loads(reports[0].read_text())["completed_legs"] == 1


def test_patrol_records_a_crashing_leg_and_stays_disarmed(tmp_path: Path) -> None:
    ctl, fake, _ = make_ctl(tmp_path, [{"ok": True}])

    def flaky(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "navigate" in command and not fake.navigate:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="TimeoutError")
        return fake(command, **kwargs)

    ctl.run = flaky
    route = parse_route({"waypoints": [{"x": 1, "y": 0}, {"x": 0, "y": 0}]})
    summary = ctl.patrol(route)
    assert summary["completed_legs"] == 1 and "TimeoutError" in summary["legs"][1]["error"]
    assert json.loads((ctl.settings.board / "arm.json").read_text()) == {"enabled": False}


def test_modes_save_and_cli(tmp_path: Path) -> None:
    ctl, fake, _ = make_ctl(tmp_path, [])
    ctl.settings.maps.rmdir()
    ctl.set_mode(mapping=True)
    assert "mapping:=true" in (ctl.settings.compose_dir / ".env").read_text()
    for folder in (ctl.settings.data, ctl.settings.maps, ctl.settings.reports):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert fake.calls[-1][-3:] == ["up", "-d", "--force-recreate"]
    with pytest.raises(ValueError, match="not found"):
        ctl.set_mode(mapping=False, name="room")
    (ctl.settings.maps / "room.yaml").write_text("image: room.pgm\n")
    ctl.set_mode(mapping=False, name="room")
    assert "map:=/maps/room.yaml" in (ctl.settings.compose_dir / ".env").read_text()
    ctl.save("room")
    assert "/maps/room" in fake.calls[-1]
    assert main(["status"], ctl) == 0
    assert main(["where"], ctl) == 0
    assert main(["stop"], ctl) == 0
    route = tmp_path / "bad.json"
    route.write_text("{}")
    with pytest.raises(ValueError):
        main(["patrol", str(route)], ctl)
    fake.navigate.append({"ok": False})
    assert main(["goal", "0.1", "0", "0"], ctl) == 2


def telemetry(volts: float, speed: float = 0.0) -> dict[str, Any]:
    return {"ok": True, "telemetry": {"L": speed, "R": speed, "v": volts}}


def test_standstill_voltage_ignores_moving_samples(tmp_path: Path) -> None:
    ctl, _, board = make_ctl(tmp_path, [])
    board.status.side_effect = [telemetry(9.0, speed=0.1)] * 3 + [
        telemetry(v) for v in (11.0, 11.2, 11.1, 30.0, 11.3)
    ]
    assert ctl.standstill_voltage() == 11.2
    board.status.side_effect = None
    board.status.return_value = telemetry(11.0, speed=0.05)
    with pytest.raises(ValueError, match="standstill"):
        ctl.standstill_voltage()


def test_low_battery_refuses_to_arm(tmp_path: Path) -> None:
    ctl, fake, board = make_ctl(tmp_path, [{"ok": True}])
    board.status.return_value = telemetry(9.9)
    with pytest.raises(BatteryLow) as low:
        ctl.goal(Pose(0.2, 0, 0))
    assert not low.value.critical and fake.armed_during_navigate == []
    assert json.loads((ctl.settings.board / "arm.json").read_text()) == {"enabled": False}
    board.status.return_value = telemetry(9.0)
    with pytest.raises(BatteryLow) as low:
        ctl.goal(Pose(0.2, 0, 0), battery_gate=9.2)
    assert low.value.critical


def localized_with_home(ctl: NavCtl) -> None:
    from ugv_nav_tools import places as placefile
    from ugv_nav_tools.map_files import sha256_file

    maps = ctl.settings.maps
    (maps / "home1.pgm").write_bytes(b"P5\n1 1\n255\n\xfe")
    marked = {
        "version": 1,
        "map": "home1",
        "map_sha256": sha256_file(maps / "home1.pgm"),
        "places": [
            {"id": "start", "name": "出发点", "kind": "home", "pose": {"x": 0, "y": 0, "yaw": 0}}
        ],
    }
    placefile.save(maps, placefile.parse(marked))
    env = "UGV_NAV_ARGS=mapping:=false localization:=true navigation:=true map:=/maps/home1.yaml\n"
    (ctl.settings.compose_dir / ".env").write_text(env)


def test_patrol_goes_only_home_when_the_battery_drops(tmp_path: Path) -> None:
    ctl, fake, board = make_ctl(tmp_path, [{"ok": True}, {"ok": True}])
    localized_with_home(ctl)
    board.status.side_effect = [telemetry(10.5)] * 5 + [telemetry(9.4)] * 5 + [telemetry(9.4)] * 5
    route = parse_route({"waypoints": [{"x": 1, "y": 0}, {"x": 0, "y": 1}, {"x": 1, "y": 1}]})
    summary = ctl.patrol(route)
    assert summary["abort_reason"] == "battery" and summary["completed_legs"] == 1
    assert summary["legs"][1]["home"]["ok"] and summary["legs"][1]["battery_v"] == 9.4
    assert len(fake.armed_during_navigate) == 2  # leg 1, then only the way home

    (tmp_path / "critical").mkdir()
    ctl, fake, board = make_ctl(tmp_path / "critical", [{"ok": True}])
    localized_with_home(ctl)
    board.status.side_effect = [telemetry(10.5)] * 5 + [telemetry(9.0)] * 5
    summary = ctl.patrol(route)
    assert summary["abort_reason"] == "battery" and "home" not in summary["legs"][1]
    assert len(fake.armed_during_navigate) == 1  # below critical: stay put


def test_patrol_reports_a_safety_fault(tmp_path: Path) -> None:
    fault = {"ok": False, "fault": {"reason": "stall_body", "at": 1.0}}
    ctl, _, _ = make_ctl(tmp_path, [fault])
    summary = ctl.patrol(parse_route({"waypoints": [{"x": 1, "y": 0}]}))
    assert summary["abort_reason"] == "stall_body"


def test_probe_uses_a_short_probe_arm_and_summarizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ugv_nav_tools import navctl as navctl_module

    ctl, _, _ = make_ctl(tmp_path, [])
    seen: dict[str, Any] = {}

    def fake_request(self: Any, payload: dict[str, Any]) -> dict[str, Any]:
        seen["arm"] = json.loads((ctl.settings.board / "arm.json").read_text())
        seen["payload"] = payload
        samples = [{"t": i / 10, "L": 0.0 if i < 5 else 0.04, "R": 0.06} for i in range(10)]
        return {
            "ok": True,
            "frame": {"T": 13},
            "targets": {"L": 0.04, "R": 0.06},
            "samples": samples,
        }

    monkeypatch.setattr(navctl_module.BoardClient, "request", fake_request)
    summary = ctl.probe("t13", 0.05, 0.1, 1.0)
    assert seen["arm"]["mode"] == "probe" and seen["arm"]["enabled"]
    assert seen["payload"] == {"op": "probe", "command": "t13", "a": 0.05, "b": 0.1, "seconds": 1.0}
    assert (
        summary["L_mean"] == 0.04 and summary["R_mean"] == 0.06 and summary["L_zero_share"] == 0.0
    )
    assert json.loads((ctl.settings.board / "arm.json").read_text()) == {"enabled": False}
