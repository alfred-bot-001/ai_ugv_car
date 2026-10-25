"""ugv-navd: the console map page's way to run ugv-navctl operations.

pi-agent's sandbox (NoNewPrivileges, read-only home) cannot run `sudo docker` or write
the compose .env, so this small host service does it as user ws, with the same NavCtl
code an operator runs over ssh. It serves one JSON request per connection on a private
socket outside the container's mounts (the container must never reach it: a request
here can open a motion window). Only a fixed set of operations is accepted and each is
validated again here; motion still needs NavCtl's bounded arm window and battery gate,
and every pi-agent check behind it.

One job runs at a time. `stop` is never queued: it closes the motion window at once
(pi-agent's watchdog then stops the wheels within 25 ms) and cancels Nav2 goals.

After the Pi boots, navd starts the container again in the mode the map page last chose
(boot_start); docker's own restart policy stays "no", so navd alone starts the stack.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import logging
import math
import os
import re
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import map_admin
from .board_client import BoardError
from .map_files import FREE_PIXEL, read_map_yaml, read_pgm
from .navctl import (
    BATTERY_CRITICAL_V,
    BATTERY_LEG_V,
    BATTERY_START_V,
    MAP_NAME,
    BatteryLow,
    Cancelled,
    NavCtl,
    Settings,
    map_name,
)
from .route import Pose, Route, parse_route, wrap

LOGGER = logging.getLogger("ugv-navd")
SOCKET = Path("/home/ws/deploy/ugv-nav/run/ops.sock")
MAX_REQUEST = 4096
# A home map is a few tens of metres at most; anything outside is a typo or an attack.
COORD_LIMIT = 100.0
CONTAINER_CACHE_S = 2.0
# After a mode switch the Nav2 lifecycle managers must report their nodes active. On the
# car (2026-10-23) map_server's reply to "configure" was lost in DDS once and the stack
# sat unconfigured; one recreate fixed it. So: wait, recreate once, then fail clearly.
READY_TIMEOUT_S = 90.0
# On a saved map Nav2's planner waits for the map frame, which AMCL publishes only after
# the initial pose: the navigation manager comes up after "set pose", not at load time
# (2026-10-24: requiring it at load time failed every load after two 90 s waits).
POSE_READY_TIMEOUT_S = 45.0
READY_POLL_S = 3.0
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
# navd starts after docker.service; this only covers a slow docker on a cold boot.
BOOT_DOCKER_WAIT_S = 120.0
# Mapping that boot_start began, with the wheels moved less than this since, holds no
# map worth keeping, so standby may stop it; otherwise a reboot meant no sleep at all.
BOOT_STILL_M = 0.2
MANAGERS = {"navigation": "导航", "localization": "定位"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")
ACTIVE = re.compile(r"\[lifecycle_manager_(\w+)\]: Managed nodes are active")
MAX_MAPS = 100
# Saved-map housekeeping, answered at once (file work only, never while a job runs).
MAP_ADMIN = ("preview", "rename", "delete", "default", "route", "route_save", "reports")
OPS: tuple[str, ...] = (
    "status",
    "maps",
    "mapping",
    "save",
    "localize",
    "pose",
    "goal",
    "explore",
    "patrol",
    "stop",
    "standby",
    "wake",
) + MAP_ADMIN
# Exploration (map page "自动探索"): frontier legs, each one ordinary goal with its own
# bounded arm window and battery gate. It ends when no frontier is left, on a safety
# stop, on cancel, after three legs in a row fail, or at these caps.
EXPLORE_MAX_LEGS = 30
EXPLORE_MAX_S = 20 * 60.0
EXPLORE_MAX_FAILURES = 3
FIELDS = {
    "save": {"map"},
    "localize": {"map"},
    "pose": {"x", "y", "yaw"},
    "goal": {"x", "y", "yaw"},
    "preview": {"map"},
    "delete": {"map"},
    "default": {"map"},
    "rename": {"map", "to"},
    "route": {"map"},
    "route_save": {"map", "route"},
}
# pi-agent's safety stop reasons (tvpet_pi.nav), as the map page shows them.
FAULT_TEXT = {
    "stall_body": "车轮在转但车没动",
    "localization_lost": "定位丢失",
    "stall_wheel_left": "左轮转不动",
    "stall_wheel_right": "右轮转不动",
    "operator_cancel": "网页取消了导航",
}
OP_TEXT = {
    "mapping": "开始建图",
    "save": "保存地图",
    "localize": "切换到已保存地图定位",
    "pose": "设置当前位置",
    "goal": "导航去目标点",
    "explore": "自动探索",
    "patrol": "巡航",
    "standby": "休眠",
    "wake": "唤醒",
}


@dataclass(frozen=True)
class Request:
    op: str
    map: str = ""
    pose: Pose | None = None
    to: str = ""
    route: Route | None = None


def number(raw: dict[str, Any], key: str, limit: float) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    if not math.isfinite(value) or abs(value) > limit:
        raise ValueError(f"{key} out of range")
    return float(value)


def parse_request(raw: Any) -> Request:
    """Exactly the fields each operation takes; names and coordinates are bounded."""
    if not isinstance(raw, dict):
        raise ValueError("request must be an object")
    op = raw.get("op")
    if op not in OPS:
        raise ValueError("unknown operation")
    extra = set(raw) - {"op"} - FIELDS.get(op, set())
    if extra:
        raise ValueError("unexpected fields: " + ", ".join(sorted(extra)))
    if op == "reports":
        return Request(op)
    if op in ("save", "localize") or op in MAP_ADMIN:
        name, to = raw.get("map"), raw.get("to", "")
        if not isinstance(name, str) or not isinstance(to, str):
            raise ValueError("map name required")
        route = None
        if op == "route_save":
            route = parse_route(raw.get("route"))
            if any(abs(p.x) > COORD_LIMIT or abs(p.y) > COORD_LIMIT for p in route.waypoints):
                raise ValueError("waypoint out of range")
        to = map_name(to) if op == "rename" else ""
        return Request(op, map=map_name(name), to=to, route=route)
    if op in ("pose", "goal"):
        x, y = number(raw, "x", COORD_LIMIT), number(raw, "y", COORD_LIMIT)
        yaw = wrap(number(raw, "yaw", 2 * math.pi))
        return Request(op, pose=Pose(x, y, yaw))
    return Request(op)


@dataclass
class Job:
    id: str
    op: str
    detail: dict[str, Any]
    started: float
    state: str = "running"  # running | ok | failed | cancelled
    finished: float | None = None
    message: str = ""
    result: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "op": self.op,
            "detail": self.detail,
            "started": round(self.started, 3),
            "state": self.state,
            "finished": None if self.finished is None else round(self.finished, 3),
            "message": self.message,
            "result": self.result,
        }


def describe(request: Request) -> dict[str, Any]:
    if request.pose is not None:
        p = request.pose
        return {"x": round(p.x, 3), "y": round(p.y, 3), "yaw": round(p.yaw, 4)}
    return {"map": request.map} if request.map else {}


# ugv-ros-op's reasons for not starting, as the map page says them.
GOAL_ERROR_TEXT = (
    (
        "planning failed",
        "没找到能走的路线。目标可能离墙太近、在没探索过的地方，或者和老三之间没有车能通过的通道",
    ),
    ("rejected goal", "导航拒绝了这个目标"),
    ("did not answer", "导航没有响应"),
    ("unavailable", "导航模块还没准备好，稍等再试"),
    ("TimeoutError", "导航响应超时"),
)


def goal_error_text(error: str) -> str:
    for needle, text in GOAL_ERROR_TEXT:
        if needle in error:
            return text
    return error[:200] or "规划失败"


def goal_summary(result: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The few numbers the page shows, and a Chinese one-liner."""
    keep = ("ok", "error", "error_m", "error_rad", "duration_s", "timeout", "path_points")
    summary = {k: result[k] for k in keep if k in result}
    fault = result.get("fault")
    if isinstance(fault, dict):
        summary["fault"] = str(fault.get("reason", ""))[:80]
    if isinstance(result.get("reached"), dict):
        summary["reached"] = {k: round(float(v), 3) for k, v in result["reached"].items()}
    if result.get("ok"):
        return summary, f"已到达，误差 {float(result.get('error_m', 0)) * 100:.0f} cm"
    if "fault" in summary:
        return summary, "已安全停车：" + FAULT_TEXT.get(summary["fault"], summary["fault"])
    if result.get("timeout"):
        return summary, "超时未到达，已停车"
    if "error_m" in result:
        return summary, f"未到达目标，停在离目标 {float(result['error_m']) * 100:.0f} cm 处"
    return summary, "没有出发：" + goal_error_text(str(result.get("error") or ""))


def env_mode(env_text: str) -> dict[str, Any]:
    """Which launch the compose .env selects: mapping, localization (with map) or odometry."""
    args = ""
    launch = ""
    for line in env_text.splitlines():
        key, _, value = line.partition("=")
        if key == "UGV_NAV_ARGS":
            args = value
        elif key == "UGV_NAV_LAUNCH":
            launch = value
    if launch != "n3_navigation":
        return {"mode": "odometry", "map": None}
    if "mapping:=true" in args:
        return {"mode": "mapping", "map": None}
    match = re.search(r"map:=/maps/([A-Za-z0-9_-]{1,64})\.yaml", args)
    return {"mode": "localization", "map": match.group(1) if match else None}


def map_summary(yaml_path: Path) -> dict[str, Any]:
    info = read_map_yaml(yaml_path)
    width, height, pixels = read_pgm(yaml_path.parent / info.image)
    free = pixels.count(FREE_PIXEL)  # map_saver writes trinary maps, negate 0
    name = yaml_path.stem
    return {
        "name": name,
        "modified": round(yaml_path.stat().st_mtime, 1),
        "width": width,
        "height": height,
        "resolution": info.resolution,
        "area_m2": round(free * info.resolution**2, 1),
        "places": (yaml_path.parent / f"{name}.places.json").exists(),
        "keepout": (yaml_path.parent / f"{name}.keepout.yaml").exists(),
    }


def list_maps(maps: Path) -> list[dict[str, Any]]:
    """Saved maps, newest first; unreadable files are listed with an error, not hidden."""
    found = []
    for path in maps.glob("*.yaml"):
        if path.name.endswith(".keepout.yaml") or not MAP_NAME.fullmatch(path.stem):
            continue
        try:
            found.append(map_summary(path))
        except (OSError, ValueError) as error:
            found.append({"name": path.stem, "modified": 0, "error": str(error)[:120]})
    found.sort(key=lambda m: m["modified"], reverse=True)
    return found[:MAX_MAPS]


def active_managers(log_text: str) -> set[str]:
    """Lifecycle managers (navigation, localization, ...) whose nodes came up."""
    return set(ACTIVE.findall(ANSI.sub("", log_text)))


def docker_state(container: str, run: Callable[..., Any] = subprocess.run) -> str:
    """'running', 'exited', ... (plus ' healthy'/' unhealthy'), or 'missing'."""
    template = "{{.State.Status}}{{if .State.Health}} {{.State.Health.Status}}{{end}}"
    try:
        done = run(
            ["sudo", "-n", "docker", "inspect", "-f", template, container],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return str(done.stdout).strip()[:40] if done.returncode == 0 else "missing"


def spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True, name="navd-job").start()


class Navd:
    def __init__(
        self,
        ctl: NavCtl,
        clock: Callable[[], float] = time.time,
        start: Callable[[Callable[[], None]], None] = spawn,
        inspect: Callable[[str], str] = docker_state,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ctl = ctl
        self.clock = clock
        self.start = start
        self.inspect = inspect
        self.monotonic = monotonic
        self.lock = threading.Lock()
        self.job: Job | None = None
        self.cancel_event = threading.Event()
        ctl.cancelled = self.cancel_event.is_set
        self.container = ("unknown", -math.inf)
        self.current: Job | None = None  # the running job, for progress updates
        # Wheel odometers (odl, odr) when boot_start began mapping; None otherwise.
        self.boot_odometer: tuple[float, float] | None = None

    def handle(self, raw: Any) -> dict[str, Any]:
        request = parse_request(raw)
        if request.op == "status":
            return {"ok": True, **self.status()}
        if request.op == "maps":
            maps = self.ctl.settings.maps
            return {"ok": True, "maps": list_maps(maps), "default": map_admin.default_map(maps)}
        if request.op in MAP_ADMIN:
            return {"ok": True, **self.admin(request)}
        if request.op == "stop":
            return {"ok": True, **self.stop()}
        if request.op == "wake":
            # Instant: the port switch alone. Mode switches wait for the device themselves.
            self.ctl.lidar_power(True)
            return {"ok": True, "lidar": self.ctl.lidar}
        if request.op == "standby" and self.container_state().startswith("running"):
            if self.mode()["mode"] == "mapping" and not self.boot_map_untouched():
                raise ValueError("正在建图，地图还没保存，不休眠")
        return {"ok": True, "job": self.submit(request).to_json()}

    def container_state(self) -> str:
        state, at = self.container
        if self.monotonic() - at > CONTAINER_CACHE_S:
            state = self.inspect(self.ctl.settings.container)
            self.container = (state, self.monotonic())
        return state

    def status(self) -> dict[str, Any]:
        try:
            mode = env_mode((self.ctl.settings.compose_dir / ".env").read_text())
        except OSError:
            mode = {"mode": "odometry", "map": None}
        with self.lock:
            job = self.job.to_json() if self.job else None
        container = self.container_state()
        return {
            "container": container,
            **mode,
            # pi-agent stays awake for this (and navd refuses standby).
            "unsaved_map": container.startswith("running")
            and mode["mode"] == "mapping"
            and not self.boot_map_untouched(),
            "job": job,
            "lidar": self.ctl.lidar,
            "now": self.clock(),
        }

    def submit(self, request: Request) -> Job:
        with self.lock:
            if self.job and self.job.state == "running":
                raise ValueError(f"「{OP_TEXT[self.job.op]}」还没完成，请稍候")
            self.cancel_event.clear()
            job = Job(uuid.uuid4().hex[:12], request.op, describe(request), self.clock())
            self.job = job
            if request.op in ("mapping", "localize", "explore", "save", "standby"):
                self.boot_odometer = None  # someone chose a mode, or the map changes
        if request.op in ("mapping", "localize"):
            self.container = ("starting", self.monotonic())
        LOGGER.info("job %s %s %s", job.id, job.op, job.detail)
        self.start(lambda: self.run(job, request))
        return job

    def run(self, job: Job, request: Request) -> None:
        state, message, result = "failed", "", dict[str, Any]()
        self.current = job
        try:
            state, message, result = self.execute(request)
        except Cancelled:
            state, message = "cancelled", "已取消，小车没有出发"
        except BatteryLow as low:
            message = f"电量不足（{low.volts:.2f} V），不出发"
        except subprocess.CalledProcessError as error:
            message = f"命令失败（退出码 {error.returncode}）"
        except (ValueError, RuntimeError, OSError, BoardError) as error:
            message = str(error)[:300] or type(error).__name__
        except Exception as error:  # noqa: BLE001 - a job thread must always finish its job
            LOGGER.exception("job %s crashed", job.id)
            message = f"{type(error).__name__}: {error}"[:300]
        if self.cancel_event.is_set() and job.op in ("goal", "explore") and state != "ok":
            state = "cancelled"
            message = message if message.startswith("已取消") else "已取消，小车已停下"
        with self.lock:
            job.state, job.message = state, message
            job.result = result or job.result
            job.finished = self.clock()
        LOGGER.info("job %s %s: %s %s", job.id, job.op, state, message)

    def execute(self, request: Request) -> tuple[str, str, dict[str, Any]]:
        ctl = self.ctl
        if request.op == "mapping":
            retried = self.switch_mode(mapping=True)
            return "ok", "已进入建图模式" + retried, {}
        if request.op == "localize":
            if not (ctl.settings.maps / f"{request.map}.yaml").exists():
                raise ValueError(f"找不到地图 {request.map}")
            retried = self.switch_mode(mapping=False, name=request.map)
            message = f"已加载地图 {request.map}{retried}，请设置老三的位置"
            return "ok", message, {"map": request.map}
        if request.op == "explore":
            return self.explore()
        if request.op == "patrol":
            return self.patrol()
        if request.op == "save":
            ctl.save(request.map)
            return "ok", f"地图已保存为 {request.map}", {"map": request.map}
        if request.op == "standby":
            return self.standby()
        assert request.pose is not None
        pose = request.pose
        if request.op == "pose":
            if env_mode((ctl.settings.compose_dir / ".env").read_text())["mode"] != "localization":
                raise ValueError("建图时不用设置位置；先切换到一张已保存的地图")
            reply = ctl.json_op("set-pose", str(pose.x), str(pose.y), str(pose.yaw))
            reached: dict[str, Any] = {
                k: round(float(reply[k]), 3) for k in ("x", "y", "yaw") if k in reply
            }
            if self.wait_ready({"navigation"}, POSE_READY_TIMEOUT_S):
                message = "已设置位置，但导航模块 45 秒内没启动好；请重新「加载并定位」"
                return "failed", message, reached
            return "ok", "已设置当前位置，可以「去这里」了", reached
        summary, message = goal_summary(ctl.goal(pose))
        return ("ok" if summary.get("ok") else "failed"), message, summary

    def admin(self, request: Request) -> dict[str, Any]:
        """Preview, rename, delete (to the trash) or mark the default saved map."""
        maps = self.ctl.settings.maps
        if request.op == "preview":
            meta, png = map_admin.preview(maps, request.map)
            return {"meta": meta, "png": base64.b64encode(png).decode()}
        if request.op == "route":
            return {"route": map_admin.read_route(maps, request.map)}
        if request.op == "reports":
            return {"reports": map_admin.list_reports(self.ctl.settings.reports)}
        with self.lock:
            if self.job and self.job.state == "running":
                raise ValueError(f"「{OP_TEXT[self.job.op]}」还没完成，请稍候")
        if request.op == "route_save":
            assert request.route is not None
            map_admin.save_route(maps, request.map, request.route)
            LOGGER.info("route for %s: %d points", request.map, len(request.route.waypoints))
            return {"saved": len(request.route.waypoints)}
        if request.op == "default":
            map_admin.set_default(maps, request.map)
            LOGGER.info("default map %s", request.map)
            return {"default": request.map}
        try:
            mode = env_mode((self.ctl.settings.compose_dir / ".env").read_text())
        except OSError:
            mode = {"mode": "odometry", "map": None}
        if mode["mode"] == "localization" and mode["map"] == request.map:
            raise ValueError("老三正在用这张地图；先加载别的地图或开始建图，再改名或删除")
        if request.op == "rename":
            moved = map_admin.rename(maps, request.map, request.to)
            LOGGER.info("renamed map %s -> %s (%s)", request.map, request.to, moved)
            return {"renamed": request.to}
        folder = map_admin.trash(maps, request.map)
        LOGGER.info("map %s moved to %s", request.map, folder)
        return {"trash": folder.name}

    def mode(self) -> dict[str, Any]:
        """The launch the compose .env selects; no file yet means odometry only."""
        try:
            return env_mode((self.ctl.settings.compose_dir / ".env").read_text())
        except OSError:
            return {"mode": "odometry", "map": None}

    def progress(self, result: dict[str, Any]) -> None:
        with self.lock:
            if self.current is not None:
                self.current.result = dict(result)

    def explore(self) -> tuple[str, str, dict[str, Any]]:
        """Drive to frontier after frontier while mapping (see EXPLORE_* above)."""
        ctl = self.ctl
        if self.mode()["mode"] != "mapping":
            raise ValueError("自动探索只能在建图时进行；先点「开始建图」")
        started = self.monotonic()
        failures, avoid = 0, list[tuple[float, float]]()
        summary: dict[str, Any] = {"legs": 0, "reached": 0, "explored_m2": None, "target": None}
        while True:
            if self.cancel_event.is_set():
                return "cancelled", "已取消探索，小车已停下", summary | {"target": None}
            if summary["legs"] >= EXPLORE_MAX_LEGS:
                return "ok", f"已走满 {EXPLORE_MAX_LEGS} 段，探索暂停", summary | {"target": None}
            if self.monotonic() - started > EXPLORE_MAX_S:
                return "ok", "已到 20 分钟上限，探索暂停", summary | {"target": None}
            reply = ctl.json_op("frontier", *(f"--avoid={x:.3f},{y:.3f}" for x, y in avoid))
            summary["explored_m2"] = reply.get("explored_m2")
            targets = reply.get("candidates") or []
            if not targets:
                area = summary["explored_m2"]
                return (
                    "ok",
                    f"探索结束：没有能安全开到的未探索边界了（已探索 {area} m²）。"
                    "窄处请用遥控补图，记得保存地图",
                    (summary | {"target": None}),
                )
            target = targets[0]
            summary["target"] = {k: target[k] for k in ("x", "y", "yaw")}
            self.progress(summary)
            gate = BATTERY_START_V if summary["legs"] == 0 else BATTERY_LEG_V
            result = ctl.goal(Pose(target["x"], target["y"], target["yaw"]), battery_gate=gate)
            summary["legs"] += 1
            if result.get("ok"):
                summary["reached"] += 1
                failures = 0
            else:
                fault = result.get("fault")
                if self.cancel_event.is_set():
                    return "cancelled", "已取消探索，小车已停下", summary | {"target": None}
                if isinstance(fault, dict):
                    reason = str(fault.get("reason", ""))
                    text = FAULT_TEXT.get(reason, reason)
                    return "failed", f"探索中止：已安全停车（{text}）", summary | {"target": None}
                avoid.append((float(target["x"]), float(target["y"])))
                failures += 1
                if failures >= EXPLORE_MAX_FAILURES:
                    message = f"连续 {failures} 段没到达，探索中止"
                    return "failed", message, summary | {"target": None}
            self.progress(summary)

    def patrol(self) -> tuple[str, str, dict[str, Any]]:
        """Run the localized map's saved route leg by leg, then write a report."""
        ctl = self.ctl
        mode = self.mode()
        if mode["mode"] != "localization" or not mode["map"]:
            raise ValueError("巡航要在已保存的地图上进行；先「加载并定位」")
        stored = map_admin.read_route(ctl.settings.maps, mode["map"])
        if stored is None:
            raise ValueError("这张地图还没有巡航路线；先在地图上加巡航点并保存")
        route = parse_route(stored)
        plan = route.legs()
        progress: dict[str, Any] = {
            "loops": route.loops,
            "points": len(route.waypoints),
            "total": len(plan),
            "done": 0,
            "reached": 0,
            "loop": 0,
            "index": 0,
            "target": None,
        }
        legs: list[dict[str, Any]] = []
        abort = ""
        for loop, index, pose in plan:
            if self.cancel_event.is_set():
                abort = "cancelled"
                break
            target = {"x": pose.x, "y": pose.y, "yaw": pose.yaw}
            progress |= {"loop": loop, "index": index, "target": target}
            self.progress(progress)
            gate = BATTERY_START_V if not legs else BATTERY_LEG_V
            try:
                result = ctl.goal(pose, battery_gate=gate)
            except Cancelled:
                abort = "cancelled"
                break
            except BatteryLow as low:
                result = {"ok": False, "error": str(low), "battery_v": round(low.volts, 2)}
                abort = "battery"
                home = None if low.critical or not legs else ctl.home_pose()
                if home is not None and not self.cancel_event.is_set():
                    try:
                        result["home"] = goal_summary(
                            ctl.goal(home, battery_gate=BATTERY_CRITICAL_V)
                        )[0]
                    except (RuntimeError, BoardError, ValueError) as error:
                        result["home"] = {"ok": False, "error": str(error)}
            except (RuntimeError, BoardError, ValueError) as error:
                result = {"ok": False, "error": str(error)[:200]}
            leg = goal_summary(result)[0] | {"loop": loop, "index": index}
            leg |= {k: result[k] for k in ("battery_v", "home") if k in result}
            legs.append(leg)
            progress["done"] += 1
            progress["reached"] += bool(leg.get("ok"))
            if not leg.get("ok"):
                if self.cancel_event.is_set():
                    abort = "cancelled"
                abort = abort or str(leg.get("fault") or "failed")
                break
            self.progress(progress)
        summary = {
            "ok": len(legs) == len(plan) and all(leg.get("ok") for leg in legs),
            "map": mode["map"],
            "completed_legs": progress["reached"],
            "planned_legs": len(plan),
            "abort_reason": abort or None,
            "legs": legs,
        }
        ctl.settings.reports.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = f"patrol-{time.strftime('%Y%m%d-%H%M%S')}.json"
        (ctl.settings.reports / name).write_text(json.dumps(summary, ensure_ascii=False, indent=1))
        result = progress | {"target": None, "report": name}
        done = f"（{progress['reached']}/{len(plan)} 段）"
        if summary["ok"]:
            return "ok", f"巡航完成：{len(plan)} 段全部到达", result
        if abort == "cancelled":
            return "cancelled", "已取消巡航" + done, result
        if abort == "battery":
            return "failed", "电量低，巡航停止" + done, result
        where = f"第 {progress['loop']} 圈第 {progress['index']} 点"
        reason = FAULT_TEXT.get(abort, "没到达")
        return "failed", f"巡航中止：{where}{reason}" + done, result

    def boot_start(self, marker: Path, boot_id: str) -> Job | None:
        """After the Pi boots, start the container in its last mode; once per boot.

        A saved map is loaded again (its pose is then set on the map page as after any
        load) and mapping starts afresh. Restarting navd itself (a deploy) finds this
        boot's id in `marker` and leaves a stopped container alone, e.g. after standby.
        """
        with contextlib.suppress(OSError):
            if marker.read_text().strip() == boot_id:
                return None
        deadline = self.monotonic() + BOOT_DOCKER_WAIT_S
        while (state := self.inspect(self.ctl.settings.container)) == "unknown":
            if self.monotonic() >= deadline:
                LOGGER.warning("boot start: docker did not answer")
                return None
            self.ctl.sleep(READY_POLL_S)
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker.write_text(boot_id + "\n")
        if state.startswith("running"):
            return None
        mode = self.mode()
        name = mode["map"]
        if (
            mode["mode"] == "localization"
            and name
            and (self.ctl.settings.maps / f"{name}.yaml").exists()
        ):
            job = self.submit(Request("localize", map=name))
        else:
            job = self.submit(Request("mapping"))
            for _ in range(5):  # pi-agent may still be opening the UART
                if (odometer := self.odometer()) is not None:
                    self.boot_odometer = odometer
                    break
                self.ctl.sleep(2.0)
        job.detail = {**job.detail, "boot": True}
        LOGGER.info("boot start: %s %s", job.op, job.detail)
        return job

    def odometer(self) -> tuple[float, float] | None:
        """Both wheels' odometers (m, T1001 odl/odr) from pi-agent, or None."""
        try:
            telemetry = self.ctl.board.status().get("telemetry") or {}
        except (BoardError, OSError, ValueError):
            return None
        left, right = telemetry.get("odl"), telemetry.get("odr")
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return float(left), float(right)
        return None

    def boot_map_untouched(self) -> bool:
        """Mapping that boot_start began, and the wheels have not moved since."""
        start = self.boot_odometer
        now = self.odometer() if start is not None else None
        if start is None or now is None:
            return False
        return max(abs(now[0] - start[0]), abs(now[1] - start[1])) < BOOT_STILL_M

    def switch_mode(self, *, mapping: bool, name: str | None = None) -> str:
        """Recreate the container and wait for its lifecycle managers; one retry."""
        needed = {"navigation"} if mapping else {"localization"}
        for attempt in (1, 2):
            self.ctl.set_mode(mapping=mapping, name=name)
            missing = self.wait_ready(needed)
            if not missing:
                return "（启动卡住，已自动重试）" if attempt == 2 else ""
            LOGGER.warning("lifecycle not active after %.0f s: %s", READY_TIMEOUT_S, missing)
        names = "、".join(MANAGERS.get(m, m) for m in sorted(missing))
        raise RuntimeError(f"导航容器的{names}模块没能启动，请再试一次")

    def wait_ready(self, needed: set[str], timeout: float = READY_TIMEOUT_S) -> set[str]:
        """Managers still not active when the wait ends (empty when all came up)."""
        deadline = self.monotonic() + timeout
        while True:
            done = self.ctl.run(
                ["sudo", "-n", "docker", "logs", self.ctl.settings.container],
                capture_output=True,
                text=True,
                check=False,
            )
            missing = needed - active_managers(str(done.stdout) + str(done.stderr))
            if not missing or self.monotonic() >= deadline:
                return missing
            self.ctl.sleep(READY_POLL_S)

    def standby(self) -> tuple[str, str, dict[str, Any]]:
        """Sleep mode (pi-agent asks after 30 idle minutes): no container, no LiDAR power.

        A running localization stack is dropped: the saved map is on disk and the map
        page loads it again after waking. Mapping (an unsaved map) is refused in handle().
        """
        parts = []
        if self.container_state().startswith("running"):
            self.ctl.stop_container()
            self.container = ("exited", self.monotonic())
            parts.append("导航容器已停止")
        self.ctl.lidar_power(False)
        parts.append("雷达已断电")
        return "ok", "，".join(parts), {"lidar": self.ctl.lidar}

    def stop(self) -> dict[str, Any]:
        """Close the motion window now; Nav2 goals are cancelled in the background."""
        self.cancel_event.set()
        self.ctl.cancel()
        with self.lock:
            running = self.job.op if self.job and self.job.state == "running" else None
        self.start(self.cancel_goals)
        LOGGER.info("stop (running job: %s)", running)
        return {"stopped": True, "running": running}

    def cancel_goals(self) -> None:
        try:
            self.ctl.json_op("cancel")
        except (RuntimeError, ValueError, OSError) as error:
            LOGGER.warning("Nav2 cancel failed: %s", error)


def respond(navd: Navd, data: bytes) -> bytes:
    try:
        reply = navd.handle(json.loads(data))
    except Exception as error:  # noqa: BLE001 - every failure is a JSON reply
        reply = {"ok": False, "error": str(error)[:300] or type(error).__name__}
    return json.dumps(reply, ensure_ascii=False, allow_nan=False).encode() + b"\n"


def serve(navd: Navd, path: Path, stopping: threading.Event) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as server:
        previous = os.umask(0o177)
        try:
            server.bind(str(path))
        finally:
            os.umask(previous)
        os.chmod(path, 0o600)
        server.listen(4)
        server.settimeout(0.5)
        while not stopping.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(1.0)
                data = b""
                with contextlib.suppress(OSError):
                    while b"\n" not in data and len(data) < MAX_REQUEST:
                        part = connection.recv(MAX_REQUEST - len(data))
                        if not part:
                            break
                        data += part
                with contextlib.suppress(OSError):
                    connection.sendall(respond(navd, data))
    path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="ugv-navd", description=__doc__.splitlines()[0])
    parser.add_argument("--socket", type=Path, default=SOCKET)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ctl = NavCtl(Settings(), echo=LOGGER.info)
    navd = Navd(ctl)
    ctl.disarm()  # a restart never inherits a motion window
    boot_id = BOOT_ID.read_text().strip()

    def start_after_boot() -> None:
        navd.boot_start(args.socket.parent / "boot.id", boot_id)

    spawn(start_after_boot)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    LOGGER.info("serving %s", args.socket)
    try:
        serve(navd, args.socket, stopping)
    finally:
        ctl.cancel()
