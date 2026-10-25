"""ugv-navd: strict requests, one job at a time, and a stop that always wins."""

import argparse
import json
import math
import os
import socket
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from ugv_nav_tools.map_files import write_pgm
from ugv_nav_tools.navctl import NavCtl, Settings
from ugv_nav_tools.navd import (
    Navd,
    active_managers,
    docker_state,
    env_mode,
    goal_summary,
    list_maps,
    parse_request,
    respond,
    serve,
)
from ugv_nav_tools.route import Pose


class FakeRun:
    """docker exec / compose calls; ugv-ros-op answers come from `replies`."""

    def __init__(self, board: Path, replies: dict[str, list[dict[str, Any]]]) -> None:
        self.board = board
        self.replies = replies
        self.calls: list[list[str]] = []
        self.armed_during_navigate: list[bool] = []
        # docker logs of the current container; each mode switch pops the next one.
        self.logs = [ACTIVE_LOGS]
        self.container_logs = ""

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        stdout = ""
        if command[-3:] == ["up", "-d", "--force-recreate"]:
            self.container_logs = self.logs.pop(0) if len(self.logs) > 1 else self.logs[0]
        if command[:4] == ["sudo", "-n", "docker", "logs"]:
            stdout = self.container_logs
        if "ugv-ros-op" in command:
            op = command[command.index("ugv-ros-op") + 1]
            if op == "navigate":
                arm = json.loads((self.board / "arm.json").read_text())
                self.armed_during_navigate.append(arm["enabled"])
            queue = self.replies.get(op)
            stdout = json.dumps(queue.pop(0) if queue else {"op": op})
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def ops(self) -> list[str]:
        return [c[c.index("ugv-ros-op") + 1] for c in self.calls if "ugv-ros-op" in c]


ACTIVE_LOGS = (
    "[lifecycle_manager-9] [INFO] [1.0] [lifecycle_manager_localization]: "
    "\x1b[34m\x1b[1mManaged nodes are active\x1b[0m\x1b[0m\n"
    "[lifecycle_manager-15] [INFO] [2.0] [lifecycle_manager_navigation]: "
    "Managed nodes are active\n"
)
STUCK_LOGS = (
    "[lifecycle_manager_localization]: Configuring map_server\n" + ACTIVE_LOGS.split("\n")[1]
)
# A saved map before the initial pose: AMCL is up, Nav2's planner waits for the map frame.
WAITING_FOR_POSE_LOGS = ACTIVE_LOGS.split("\n")[0] + "\n"


class Deferred:
    """Collects job threads so a test decides when (and whether) they run."""

    def __init__(self) -> None:
        self.pending: list[Callable[[], None]] = []

    def __call__(self, target: Callable[[], None]) -> None:
        self.pending.append(target)

    def run_all(self) -> None:
        while self.pending:
            self.pending.pop(0)()


def make_navd(
    tmp_path: Path, replies: dict[str, list[dict[str, Any]]] | None = None, volts: float = 12.0
) -> tuple[Navd, FakeRun, Deferred]:
    (tmp_path / "ugv_lidar").write_text("")  # the LiDAR is plugged in and powered
    settings = Settings(
        deploy=tmp_path / "deploy", board=tmp_path / "board", data=tmp_path / "data",
        lidar_device=tmp_path / "ugv_lidar",
    )  # fmt: skip
    settings.board.mkdir()
    settings.compose_dir.mkdir(parents=True)
    settings.maps.mkdir(parents=True)
    run = FakeRun(settings.board, replies or {})
    board = Mock()
    board.status.return_value = {"ok": True, "telemetry": {"L": 0.0, "R": 0.0, "v": volts}}
    ctl = NavCtl(settings, run=run, board=board, echo=lambda _: None, sleep=lambda _: None)
    deferred = Deferred()
    clock = iter(range(1000, 10**6))
    ticks = iter(range(0, 10**6, 10))  # each look at the clock is 10 s later
    navd = Navd(
        ctl,
        clock=lambda: float(next(clock)),
        start=deferred,
        inspect=lambda _: "running healthy",
        monotonic=lambda: float(next(ticks)),
    )
    return navd, run, deferred


def write_map(maps: Path, name: str, free: int = 10) -> None:
    pixels = bytes([254] * free + [0] * 5 + [205] * (100 - free - 5))
    write_pgm(maps / f"{name}.pgm", 10, 10, pixels)
    (maps / f"{name}.yaml").write_text(
        f"image: {name}.pgm\nresolution: 0.05\norigin: [-1.0, -2.0, 0]\nnegate: 0\n"
        "occupied_thresh: 0.65\nfree_thresh: 0.196\n"
    )


def localized(navd: Navd, name: str = "room") -> None:
    env = navd.ctl.settings.compose_dir / ".env"
    env.write_text(
        "UGV_NAV_LAUNCH=n3_navigation\nUGV_NAV_ARGS=mapping:=false localization:=true "
        f"navigation:=true map:=/maps/{name}.yaml\n"
    )


def test_requests_take_exactly_their_fields() -> None:
    assert parse_request({"op": "status"}).op == "status"
    assert parse_request({"op": "save", "map": "room-1"}).map == "room-1"
    goal = parse_request({"op": "goal", "x": 1, "y": -2.5, "yaw": 4.0}).pose
    assert goal is not None and (goal.x, goal.y) == (1.0, -2.5)
    assert goal.yaw == pytest.approx(4.0 - 2 * math.pi)
    for bad in (
        [],
        {"op": "shell"},
        {"op": "status", "x": 1},
        {"op": "save"},
        {"op": "save", "map": "../etc"},
        {"op": "localize", "map": 5},
        {"op": "goal", "x": 1, "y": 0},
        {"op": "goal", "x": True, "y": 0, "yaw": 0},
        {"op": "goal", "x": math.nan, "y": 0, "yaw": 0},
        {"op": "goal", "x": 101, "y": 0, "yaw": 0},
        {"op": "pose", "x": 0, "y": 0, "yaw": 7},
        {"op": "goal", "x": 0, "y": 0, "yaw": 0, "speed": 1},
    ):
        with pytest.raises(ValueError):
            parse_request(bad)


def test_env_mode_and_container_state() -> None:
    assert env_mode("UGV_NAV_LAUNCH=n1_odometry\n")["mode"] == "odometry"
    mapping = "UGV_NAV_LAUNCH=n3_navigation\nUGV_NAV_ARGS=mapping:=true localization:=false\n"
    assert env_mode(mapping) == {"mode": "mapping", "map": None}
    local = "UGV_NAV_LAUNCH=n3_navigation\nUGV_NAV_ARGS=mapping:=false map:=/maps/a-1.yaml\n"
    assert env_mode(local) == {"mode": "localization", "map": "a-1"}

    def run(command: list[str], **kwargs: Any) -> Any:
        assert command[:4] == ["sudo", "-n", "docker", "inspect"] and command[-1] == "ugv-nav"
        return subprocess.CompletedProcess(command, 0, stdout="running healthy\n")

    assert docker_state("ugv-nav", run) == "running healthy"
    assert docker_state("x", lambda c, **k: subprocess.CompletedProcess(c, 1, "")) == "missing"

    def broken(command: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(command, 5)

    assert docker_state("x", broken) == "unknown"


def test_maps_are_listed_newest_first_without_masks(tmp_path: Path) -> None:
    write_map(tmp_path, "old", free=20)
    write_map(tmp_path, "new")
    os.utime(tmp_path / "old.yaml", (1, 1))
    (tmp_path / "new.keepout.yaml").write_text("image: x.pgm\n")
    (tmp_path / "new.places.json").write_text("{}")
    (tmp_path / "broken.yaml").write_text("nonsense\n")
    (tmp_path / "bad name.yaml").write_text("image: x.pgm\n")
    maps = list_maps(tmp_path)
    assert [m["name"] for m in maps] == ["new", "old", "broken"]
    assert maps[0]["places"] and maps[0]["keepout"] and not maps[1]["places"]
    assert maps[0]["width"] == 10 and maps[1]["area_m2"] == 0.1  # 20 free cells of 5 cm
    assert "error" in maps[2]


def test_mode_jobs_run_one_at_a_time(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path)
    reply = navd.handle({"op": "mapping"})
    assert reply["job"]["state"] == "running" and navd.container[0] == "starting"
    with pytest.raises(ValueError, match="还没完成"):
        navd.handle({"op": "save", "map": "room"})
    deferred.run_all()
    status = navd.status()
    assert status["job"]["state"] == "ok" and status["mode"] == "mapping"
    assert ["up", "-d", "--force-recreate"] in [c[-3:] for c in run.calls]
    navd.handle({"op": "save", "map": "room"})
    deferred.run_all()
    assert navd.job is not None and navd.job.message == "地图已保存为 room"
    assert any("/maps/room" in part for part in run.calls[-1])
    navd.handle({"op": "localize", "map": "missing"})
    deferred.run_all()
    assert navd.job.state == "failed" and "找不到地图" in navd.job.message


def test_pose_only_on_a_saved_map(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path, {"set-pose": [{"x": 1.0, "y": 2.0, "yaw": 0.5}]})
    (navd.ctl.settings.compose_dir / ".env").write_text(
        "UGV_NAV_LAUNCH=n3_navigation\nUGV_NAV_ARGS=mapping:=true\n"
    )
    navd.handle({"op": "pose", "x": 1, "y": 2, "yaw": 0.5})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "failed" and "建图时" in navd.job.message
    localized(navd)
    run.container_logs = ACTIVE_LOGS
    navd.handle({"op": "pose", "x": 1, "y": 2, "yaw": 0.5})
    deferred.run_all()
    assert navd.job.state == "ok" and navd.job.result == {"x": 1.0, "y": 2.0, "yaw": 0.5}
    assert run.ops()[-1] == "set-pose"


def test_goal_reports_and_always_disarms(tmp_path: Path) -> None:
    arrived = {"ok": True, "error_m": 0.04, "duration_s": 12.5, "reached": {"x": 1, "y": 0}}
    stuck = {"ok": False, "fault": {"reason": "stall_body", "at": 1}, "error_m": 0.6}
    navd, run, deferred = make_navd(tmp_path, {"navigate": [arrived, stuck]})
    navd.handle({"op": "goal", "x": 1, "y": 0, "yaw": 0})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "ok"
    assert navd.job.message == "已到达，误差 4 cm"
    assert run.armed_during_navigate == [True]
    assert json.loads((navd.ctl.settings.board / "arm.json").read_text()) == {"enabled": False}
    navd.handle({"op": "goal", "x": 1, "y": 0, "yaw": 0})
    deferred.run_all()
    assert navd.job.state == "failed" and navd.job.result["fault"] == "stall_body"
    assert navd.job.message == "已安全停车：车轮在转但车没动"


def test_low_battery_goal_never_arms(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path, volts=9.8)
    navd.handle({"op": "goal", "x": 1, "y": 0, "yaw": 0})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "failed" and "电量不足" in navd.job.message
    assert "navigate" not in run.ops()


def test_stop_before_arming_keeps_the_car_still(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path, {"navigate": [{"ok": True}]})
    navd.handle({"op": "goal", "x": 1, "y": 0, "yaw": 0})
    reply = navd.handle({"op": "stop"})
    assert reply == {"ok": True, "stopped": True, "running": "goal"}
    assert json.loads((navd.ctl.settings.board / "arm.json").read_text()) == {"enabled": False}
    deferred.run_all()  # the goal thread, then the Nav2 cancel
    assert navd.job is not None and navd.job.state == "cancelled"
    assert run.armed_during_navigate == [] and run.ops()[-1] == "cancel"
    # A new job starts from a clean slate.
    navd.handle({"op": "goal", "x": 1, "y": 0, "yaw": 0})
    deferred.run_all()
    assert navd.job.state == "ok" and run.armed_during_navigate == [True]


def test_stop_during_navigation_marks_the_job_cancelled(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path)

    def navigate_then_cancel(command: list[str], **kwargs: Any) -> Any:
        if "navigate" in command:
            navd.handle({"op": "stop"})  # the operator presses 取消 mid-run
            reply = {"ok": False, "fault": {"reason": "operator_cancel"}, "error_m": 0.5}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(reply))
        return run(command, **kwargs)

    navd.ctl.run = navigate_then_cancel
    navd.handle({"op": "goal", "x": 1, "y": 0, "yaw": 0})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "cancelled"
    assert navd.job.message == "已取消，小车已停下"


def test_goal_summary_wording() -> None:
    assert goal_summary({"ok": False, "timeout": True, "error_m": 1})[1] == "超时未到达，已停车"
    assert "规划失败" in goal_summary({"ok": False})[1]
    planning = goal_summary({"ok": False, "error": "planning failed; motion not attempted"})[1]
    assert planning.startswith("没有出发：没找到能走的路线")
    assert (
        "还没准备好" in goal_summary({"ok": False, "error": "RuntimeError: planner unavailable"})[1]
    )
    assert goal_summary({"ok": False, "error_m": 0.3})[1] == "未到达目标，停在离目标 30 cm 处"


def test_crashing_job_still_finishes(tmp_path: Path) -> None:
    navd, _, deferred = make_navd(tmp_path)

    def explode(**kwargs: Any) -> None:
        raise KeyError("boom")

    navd.ctl.set_mode = explode  # type: ignore[method-assign]
    navd.handle({"op": "mapping"})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "failed" and "KeyError" in navd.job.message


def test_socket_is_private_and_answers_json(tmp_path: Path) -> None:
    navd, _, _ = make_navd(tmp_path)
    assert json.loads(respond(navd, b"not json"))["ok"] is False
    assert json.loads(respond(navd, b'{"op": "maps"}')) == {"ok": True, "maps": [], "default": None}
    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        path = Path(short) / "run" / "ops.sock"
        stopping = threading.Event()
        thread = threading.Thread(target=serve, args=(navd, path, stopping), daemon=True)
        thread.start()
        for _ in range(100):
            if path.exists():
                break
            time.sleep(0.01)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))
            client.sendall(b'{"op": "status"}\n')
            reply = json.loads(client.makefile().readline())
        assert reply["ok"] and reply["container"] == "running healthy" and reply["job"] is None
        stopping.set()
        thread.join(timeout=2)
        assert not path.exists()


def test_active_managers_ignore_colors_and_other_lines() -> None:
    assert active_managers(ACTIVE_LOGS) == {"localization", "navigation"}
    assert active_managers(STUCK_LOGS) == {"navigation"}
    assert active_managers("") == set()


def test_a_stuck_bringup_is_recreated_once(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path)
    write_map(navd.ctl.settings.maps, "room")
    run.logs = [STUCK_LOGS, ACTIVE_LOGS]
    navd.handle({"op": "localize", "map": "room"})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "ok"
    assert navd.job.message == "已加载地图 room（启动卡住，已自动重试），请设置老三的位置"
    assert sum(c[-3:] == ["up", "-d", "--force-recreate"] for c in run.calls) == 2


def test_a_bringup_stuck_twice_fails_clearly(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path)
    write_map(navd.ctl.settings.maps, "room")
    run.logs = [STUCK_LOGS]
    navd.handle({"op": "localize", "map": "room"})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "failed"
    assert navd.job.message == "导航容器的定位模块没能启动，请再试一次"
    navd.handle({"op": "mapping"})  # mapping only needs the navigation manager
    deferred.run_all()
    assert navd.job.state == "ok" and navd.job.message == "已进入建图模式"


def mapping_env(navd: Navd) -> None:
    (navd.ctl.settings.compose_dir / ".env").write_text(
        "UGV_NAV_LAUNCH=n3_navigation\nUGV_NAV_ARGS=mapping:=true localization:=false\n"
    )


def frontier(*targets: tuple[float, float], area: float = 4.2) -> dict[str, Any]:
    found = [
        {"x": x, "y": y, "yaw": 0.5, "frontier_m": 0.6, "travel_m": 1.0, "fx": x + 0.4, "fy": y}
        for x, y in targets
    ]
    return {"ok": True, "explored_m2": area, "candidates": found}


def test_explore_drives_frontier_to_frontier_until_none_is_left(tmp_path: Path) -> None:
    replies = {
        "frontier": [frontier((1, 0)), frontier((2, 1), area=6.0), frontier(area=8.5)],
        "navigate": [{"ok": True}, {"ok": True}],
    }
    navd, run, deferred = make_navd(tmp_path, replies)
    mapping_env(navd)
    navd.handle({"op": "explore"})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "ok"
    assert navd.job.message.startswith("探索结束") and "8.5 m²" in navd.job.message
    assert navd.job.result == {
        "legs": 2,
        "reached": 2,
        "seen_early": 0,
        "explored_m2": 8.5,
        "target": None,
    }
    assert run.armed_during_navigate == [True, True]
    assert json.loads((navd.ctl.settings.board / "arm.json").read_text()) == {"enabled": False}
    navigate = [c for c in run.calls if "navigate" in c]
    # Exploration legs watch their frontier piece and may end early.
    assert [c[-1] for c in navigate] == [
        "--watch-frontier=1.400,0.000",
        "--watch-frontier=2.400,1.000",
    ]


def test_explore_counts_a_frontier_mapped_on_the_way_as_done(tmp_path: Path) -> None:
    seen = {"ok": False, "seen": {"frontier_left_m": 0.1}, "error_m": 0.8}
    replies = {
        "frontier": [frontier((1, 0)), frontier((1, 0)), frontier((2, 1)), frontier()],
        "navigate": [seen, seen, {"ok": True}],
    }
    navd, run, deferred = make_navd(tmp_path, replies)
    mapping_env(navd)
    navd.handle({"op": "explore"})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "ok"
    assert navd.job.result["legs"] == 3 and navd.job.result["reached"] == 3
    assert navd.job.result["seen_early"] == 2
    frontier_calls = [c for c in run.calls if "frontier" in c]
    assert not any("--avoid" in " ".join(c) for c in frontier_calls)  # not a failure


def test_a_negative_frontier_point_reaches_argparse_as_one_token() -> None:
    """0.8.0 sent "--watch-frontier -0.054,0.488"; argparse took the value for an option."""
    parser = argparse.ArgumentParser()
    parser.add_argument("x", type=float)
    parser.add_argument("--watch-frontier")
    ctl = make_navd(Path(tempfile.mkdtemp()), {"navigate": [{"ok": True}]})[0].ctl
    calls: list[list[str]] = []
    real_run = ctl.run

    def record(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return real_run(command, **kwargs)

    ctl.run = record
    ctl.goal(Pose(-0.5, 0.2, 0.0), watch_frontier=(-0.054, 0.488))
    navigate = next(c for c in calls if "navigate" in c)
    args = navigate[navigate.index("navigate") + 1 :]
    parsed = parser.parse_args([args[0], *[a for a in args if a.startswith("--watch")]])
    assert parsed.watch_frontier == "-0.054,0.488" and parsed.x == -0.5


def test_explore_avoids_failed_targets_and_stops_on_a_safety_stop(tmp_path: Path) -> None:
    replies = {
        "frontier": [frontier((1, 0)), frontier((-1.5, 2))],
        "navigate": [
            {"ok": False, "error_m": 0.4},
            {"ok": False, "fault": {"reason": "stall_body"}},
        ],
    }
    navd, run, deferred = make_navd(tmp_path, replies)
    mapping_env(navd)
    navd.handle({"op": "explore"})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "failed"
    assert navd.job.message == "探索中止：已安全停车（车轮在转但车没动）"
    frontier_calls = [c for c in run.calls if "frontier" in c]
    assert "--avoid=1.000,0.000" in frontier_calls[1]  # the failed target is not retried


def test_explore_gives_up_after_three_misses_and_needs_mapping(tmp_path: Path) -> None:
    miss = {"ok": False, "error_m": 0.5}
    replies = {
        "frontier": [frontier((1, 0)), frontier((2, 0)), frontier((3, 0))],
        "navigate": [miss, miss, miss],
    }
    navd, _, deferred = make_navd(tmp_path, replies)
    localized(navd)
    navd.handle({"op": "explore"})
    deferred.run_all()
    assert navd.job is not None and "只能在建图时进行" in navd.job.message
    mapping_env(navd)
    navd.handle({"op": "explore"})
    deferred.run_all()
    assert navd.job.state == "failed" and navd.job.message == "连续 3 段没到达，探索中止"
    assert navd.job.result["legs"] == 3 and navd.job.result["reached"] == 0


def test_explore_stops_when_cancelled(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path, {"frontier": [frontier((1, 0))]})
    mapping_env(navd)
    navd.handle({"op": "explore"})
    navd.handle({"op": "stop"})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "cancelled"
    assert run.armed_during_navigate == []


def test_map_admin_requests(tmp_path: Path) -> None:
    navd, _, deferred = make_navd(tmp_path)
    maps = navd.ctl.settings.maps
    write_map(maps, "room")
    write_map(maps, "study")
    assert navd.handle({"op": "default", "map": "room"}) == {"ok": True, "default": "room"}
    listed = navd.handle({"op": "maps"})
    assert listed["default"] == "room" and {m["name"] for m in listed["maps"]} == {"room", "study"}
    png = navd.handle({"op": "preview", "map": "room"})
    assert png["meta"]["width"] == 10 and png["png"].startswith("iVBOR")
    assert navd.handle({"op": "rename", "map": "room", "to": "living"}) == {
        "ok": True,
        "renamed": "living",
    }
    assert navd.handle({"op": "maps"})["default"] == "living"
    localized(navd, "study")
    with pytest.raises(ValueError, match="正在用这张地图"):
        navd.handle({"op": "delete", "map": "study"})
    trashed = navd.handle({"op": "delete", "map": "living"})
    assert trashed["trash"].startswith("living-")
    assert [m["name"] for m in navd.handle({"op": "maps"})["maps"]] == ["study"]
    navd.handle({"op": "mapping"})  # a running job blocks housekeeping
    with pytest.raises(ValueError, match="还没完成"):
        navd.handle({"op": "default", "map": "study"})
    deferred.run_all()
    for bad in (
        {"op": "rename", "map": "study"},
        {"op": "rename", "map": "study", "to": "../x"},
        {"op": "delete", "map": "study", "to": "x"},
    ):
        with pytest.raises(ValueError):
            navd.handle(bad)


ROUTE = {"loops": 2, "waypoints": [{"x": 1, "y": 0, "yaw": 0}, {"x": 0, "y": 1}]}


def test_routes_are_saved_per_map_and_checked(tmp_path: Path) -> None:
    navd, _, _ = make_navd(tmp_path)
    write_map(navd.ctl.settings.maps, "room")
    assert navd.handle({"op": "route", "map": "room"}) == {"ok": True, "route": None}
    assert navd.handle({"op": "route_save", "map": "room", "route": ROUTE})["saved"] == 2
    stored = navd.handle({"op": "route", "map": "room"})["route"]
    assert stored["map"] == "room" and stored["loops"] == 2
    assert stored["waypoints"][1] == {"x": 0.0, "y": 1.0, "yaw": 0.0}
    for bad in (
        {"op": "route_save", "map": "room", "route": {"waypoints": []}},
        {"op": "route_save", "map": "room", "route": {**ROUTE, "loops": 11}},
        {"op": "route_save", "map": "room", "route": {"waypoints": [{"x": 500, "y": 0}]}},
        {"op": "route_save", "map": "missing", "route": ROUTE},
    ):
        with pytest.raises(ValueError):
            navd.handle(bad)
    navd.handle({"op": "rename", "map": "room", "to": "hall"})
    assert navd.handle({"op": "route", "map": "hall"})["route"]["map"] == "hall"


def test_patrol_runs_the_saved_route_and_writes_a_report(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path, {"navigate": [{"ok": True}] * 4})
    write_map(navd.ctl.settings.maps, "room")
    navd.handle({"op": "patrol"})
    deferred.run_all()
    assert navd.job is not None and "已保存的地图上" in navd.job.message
    localized(navd)
    navd.handle({"op": "patrol"})
    deferred.run_all()
    assert navd.job.state == "failed" and "还没有巡航路线" in navd.job.message
    navd.handle({"op": "route_save", "map": "room", "route": ROUTE})
    navd.handle({"op": "patrol"})
    deferred.run_all()
    assert navd.job.state == "ok" and navd.job.message == "巡航完成：4 段全部到达"
    assert navd.job.result["reached"] == 4 and navd.job.result["target"] is None
    assert run.armed_during_navigate == [True] * 4
    reports = navd.handle({"op": "reports"})["reports"]
    assert reports[0]["ok"] and reports[0]["completed_legs"] == 4 and reports[0]["map"] == "room"


def test_patrol_stops_at_a_failed_leg_or_low_battery(tmp_path: Path) -> None:
    stall = {"ok": False, "fault": {"reason": "stall_body"}, "error_m": 0.3}
    navd, _, deferred = make_navd(tmp_path, {"navigate": [{"ok": True}, stall]})
    write_map(navd.ctl.settings.maps, "room")
    localized(navd)
    navd.handle({"op": "route_save", "map": "room", "route": ROUTE})
    navd.handle({"op": "patrol"})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "failed"
    assert navd.job.message == "巡航中止：第 1 圈第 2 点车轮在转但车没动（1/4 段）"
    report = navd.handle({"op": "reports"})["reports"][0]
    assert report["abort_reason"] == "stall_body" and report["completed_legs"] == 1
    (tmp_path / "low").mkdir()
    low, _, deferred = make_navd(tmp_path / "low", volts=9.8)
    write_map(low.ctl.settings.maps, "room")
    localized(low)
    low.handle({"op": "route_save", "map": "room", "route": ROUTE})
    low.handle({"op": "patrol"})
    deferred.run_all()
    assert low.job is not None and low.job.message == "电量低，巡航停止（0/4 段）"


def test_patrol_can_be_cancelled(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path, {"navigate": [{"ok": True}] * 4})
    write_map(navd.ctl.settings.maps, "room")
    localized(navd)
    navd.handle({"op": "route_save", "map": "room", "route": ROUTE})
    navd.handle({"op": "patrol"})
    navd.handle({"op": "stop"})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "cancelled"
    assert navd.job.message.startswith("已取消巡航") and run.armed_during_navigate == []


def test_a_saved_map_loads_before_navigation_waits_for_the_pose(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path, {"set-pose": [{"x": 1.0, "y": 2.0, "yaw": 0.5}] * 2})
    write_map(navd.ctl.settings.maps, "room")
    run.logs = [WAITING_FOR_POSE_LOGS]
    navd.handle({"op": "localize", "map": "room"})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "ok"
    assert sum(c[-3:] == ["up", "-d", "--force-recreate"] for c in run.calls) == 1  # no retry
    navd.handle({"op": "pose", "x": 1, "y": 2, "yaw": 0.5})  # navigation never comes up
    deferred.run_all()
    assert navd.job.state == "failed" and "导航模块 45 秒内没启动好" in navd.job.message
    run.container_logs = ACTIVE_LOGS  # after the pose Nav2 finishes its bringup
    navd.handle({"op": "pose", "x": 1, "y": 2, "yaw": 0.5})
    deferred.run_all()
    assert navd.job.state == "ok" and navd.job.message == "已设置当前位置，可以「去这里」了"


def lidar_calls(run: FakeRun) -> list[str]:
    return [c[-1] for c in run.calls if c[:3] == ["sudo", "-n", "uhubctl"]]


def test_standby_stops_the_container_and_cuts_lidar_power(tmp_path: Path) -> None:
    """Sleep mode: the LiDAR motor only stops when its hub port loses 5 V."""
    navd, run, deferred = make_navd(tmp_path)
    write_map(navd.ctl.settings.maps, "room")
    localized(navd)
    assert navd.status()["lidar"] == "unknown"
    reply = navd.handle({"op": "standby"})
    assert reply["job"]["op"] == "standby" and reply["job"]["state"] == "running"
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "ok"
    assert "雷达已断电" in navd.job.message and "导航容器已停止" in navd.job.message
    assert ["sudo", "-n", "docker", "compose", "stop"] in run.calls
    assert lidar_calls(run) == ["off"]
    assert navd.status()["lidar"] == "off"


def test_standby_without_a_running_container_only_cuts_power(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path)
    navd.inspect = lambda _: "exited"
    navd.container = ("exited", 0.0)
    navd.handle({"op": "standby"})
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "ok"
    assert "容器" not in navd.job.message and lidar_calls(run) == ["off"]
    assert not any(c[-1] == "stop" for c in run.calls if "compose" in c)


def test_standby_refuses_while_mapping_or_busy(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path)
    navd.handle({"op": "mapping"})
    with pytest.raises(ValueError, match="还没完成"):
        navd.handle({"op": "standby"})
    deferred.run_all()  # mapping mode, container running: the map in memory is unsaved
    with pytest.raises(ValueError, match="正在建图"):
        navd.handle({"op": "standby"})
    assert lidar_calls(run) == ["on"]  # only set_mode's power-on
    assert navd.status()["lidar"] == "on"


def odometers(navd: Navd, left: float, right: float) -> None:
    navd.ctl.board.status.return_value = {  # type: ignore[attr-defined]
        "ok": True,
        "telemetry": {"L": 0.0, "R": 0.0, "v": 12.0, "odl": left, "odr": right},
    }


def test_boot_start_brings_the_last_mode_back_once_per_boot(tmp_path: Path) -> None:
    """A reboot left the container exited for 15 h (2026-10-25): navd starts it now."""
    navd, run, deferred = make_navd(tmp_path)
    marker = tmp_path / "run" / "boot.id"
    write_map(navd.ctl.settings.maps, "room")
    localized(navd)
    navd.inspect = lambda _: "exited"
    job = navd.boot_start(marker, "boot-1")
    assert job is not None and job.op == "localize" and job.detail == {"map": "room", "boot": True}
    assert marker.read_text() == "boot-1\n"
    deferred.run_all()
    assert navd.job is not None and navd.job.state == "ok" and "room" in navd.job.message
    assert ["sudo", "-n", "docker", "compose", "up", "-d", "--force-recreate"] in run.calls
    # Restarting navd in the same boot (a deploy) leaves the container alone.
    assert navd.boot_start(marker, "boot-1") is None and not deferred.pending
    # The next boot starts it again; a saved map that is gone means mapping instead.
    (navd.ctl.settings.maps / "room.yaml").unlink()
    job = navd.boot_start(marker, "boot-2")
    assert job is not None and job.op == "mapping" and job.detail == {"boot": True}


def test_boot_start_waits_for_docker_and_skips_a_running_container(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path)
    marker = tmp_path / "boot.id"
    answers = iter(["unknown", "unknown", "running healthy"])
    navd.inspect = lambda _: next(answers)
    assert navd.boot_start(marker, "boot-1") is None
    assert marker.read_text() == "boot-1\n" and not deferred.pending and not run.calls
    navd.inspect = lambda _: "unknown"  # docker never answers: no marker, try next start
    assert navd.boot_start(tmp_path / "other.id", "boot-1") is None
    assert not (tmp_path / "other.id").exists()


def test_standby_may_stop_a_boot_mapping_run_until_the_wheels_move(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path)
    mapping_env(navd)
    navd.inspect = lambda _: "exited"
    odometers(navd, 3.0, 2.0)
    job = navd.boot_start(tmp_path / "boot.id", "boot-1")
    assert job is not None and job.op == "mapping" and navd.boot_odometer == (3.0, 2.0)
    deferred.run_all()
    navd.inspect = lambda _: "running healthy"
    navd.container = ("running healthy", math.inf)  # cached until the end of the test
    odometers(navd, 3.25, 2.0)  # driven by hand on the joystick: a map worth keeping
    with pytest.raises(ValueError, match="正在建图"):
        navd.handle({"op": "standby"})
    odometers(navd, 3.1, 2.1)  # still where it booted: nothing to lose
    assert navd.handle({"op": "standby"})["job"]["op"] == "standby"
    deferred.run_all()
    assert navd.boot_odometer is None and lidar_calls(run) == ["on", "off"]
    # A mapping run someone chose is protected as before.
    navd.boot_odometer = (3.1, 2.1)
    navd.handle({"op": "mapping"})
    deferred.run_all()
    with pytest.raises(ValueError, match="正在建图"):
        navd.handle({"op": "standby"})


def test_wake_powers_the_lidar_on_at_once(tmp_path: Path) -> None:
    navd, run, deferred = make_navd(tmp_path)
    reply = navd.handle({"op": "wake"})
    assert reply == {"ok": True, "lidar": "on"}
    assert lidar_calls(run) == ["on"] and not deferred.pending
    assert navd.status()["lidar"] == "on"
    with pytest.raises(ValueError):
        navd.handle({"op": "wake", "map": "room"})
