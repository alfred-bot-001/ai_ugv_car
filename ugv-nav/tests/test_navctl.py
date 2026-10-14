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

from ugv_nav_tools.navctl import NavCtl, Settings, main, map_name, mode_env, write_arm
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
    assert not within_tolerance(Pose(0.08, 0, 0), Pose(0, 0, 0))
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
    settings = Settings(deploy=tmp_path / "deploy", board=tmp_path / "board")
    settings.board.mkdir()
    (settings.compose_dir).mkdir(parents=True)
    settings.maps.mkdir()
    fake = FakeRos(navigate, settings.board)
    board = Mock()
    board.status.return_value = {"ok": True, "age": 0.1, "estopped": False, "nav_owned": False}
    return NavCtl(settings, run=fake, board=board, echo=lambda _: None), fake, board


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


def test_modes_save_and_cli(tmp_path: Path) -> None:
    ctl, fake, _ = make_ctl(tmp_path, [])
    ctl.set_mode(mapping=True)
    assert "mapping:=true" in (ctl.settings.compose_dir / ".env").read_text()
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
