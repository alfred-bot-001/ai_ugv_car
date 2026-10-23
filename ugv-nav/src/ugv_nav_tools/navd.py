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
"""

from __future__ import annotations

import argparse
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

from .board_client import BoardError
from .map_files import FREE_PIXEL, read_map_yaml, read_pgm
from .navctl import MAP_NAME, BatteryLow, Cancelled, NavCtl, Settings, map_name
from .route import Pose, wrap

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
READY_POLL_S = 3.0
MANAGERS = {"navigation": "导航", "localization": "定位"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")
ACTIVE = re.compile(r"\[lifecycle_manager_(\w+)\]: Managed nodes are active")
MAX_MAPS = 100
OPS = ("status", "maps", "mapping", "save", "localize", "pose", "goal", "stop")
FIELDS = {
    "save": {"map"},
    "localize": {"map"},
    "pose": {"x", "y", "yaw"},
    "goal": {"x", "y", "yaw"},
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
}


@dataclass(frozen=True)
class Request:
    op: str
    map: str = ""
    pose: Pose | None = None


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
    if op in ("save", "localize"):
        name = raw.get("map")
        if not isinstance(name, str):
            raise ValueError("map name required")
        return Request(op, map=map_name(name))
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
    return summary, "未能出发：" + str(result.get("error") or "规划失败")[:200]


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

    def handle(self, raw: Any) -> dict[str, Any]:
        request = parse_request(raw)
        if request.op == "status":
            return {"ok": True, **self.status()}
        if request.op == "maps":
            return {"ok": True, "maps": list_maps(self.ctl.settings.maps)}
        if request.op == "stop":
            return {"ok": True, **self.stop()}
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
        return {"container": self.container_state(), **mode, "job": job, "now": self.clock()}

    def submit(self, request: Request) -> Job:
        with self.lock:
            if self.job and self.job.state == "running":
                raise ValueError(f"「{OP_TEXT[self.job.op]}」还没完成，请稍候")
            self.cancel_event.clear()
            job = Job(uuid.uuid4().hex[:12], request.op, describe(request), self.clock())
            self.job = job
        if request.op in ("mapping", "localize"):
            self.container = ("starting", self.monotonic())
        LOGGER.info("job %s %s %s", job.id, job.op, job.detail)
        self.start(lambda: self.run(job, request))
        return job

    def run(self, job: Job, request: Request) -> None:
        state, message, result = "failed", "", dict[str, Any]()
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
        if self.cancel_event.is_set() and job.op == "goal" and state != "ok":
            state = "cancelled"
            message = message if message.startswith("已取消") else "已取消，小车已停下"
        with self.lock:
            job.state, job.message, job.result = state, message, result
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
        if request.op == "save":
            ctl.save(request.map)
            return "ok", f"地图已保存为 {request.map}", {"map": request.map}
        assert request.pose is not None
        pose = request.pose
        if request.op == "pose":
            if env_mode((ctl.settings.compose_dir / ".env").read_text())["mode"] != "localization":
                raise ValueError("建图时不用设置位置；先切换到一张已保存的地图")
            reply = ctl.json_op("set-pose", str(pose.x), str(pose.y), str(pose.yaw))
            reached: dict[str, Any] = {
                k: round(float(reply[k]), 3) for k in ("x", "y", "yaw") if k in reply
            }
            return "ok", "已设置当前位置", reached
        summary, message = goal_summary(ctl.goal(pose))
        return ("ok" if summary.get("ok") else "failed"), message, summary

    def switch_mode(self, *, mapping: bool, name: str | None = None) -> str:
        """Recreate the container and wait for its lifecycle managers; one retry."""
        needed = {"navigation"} if mapping else {"navigation", "localization"}
        for attempt in (1, 2):
            self.ctl.set_mode(mapping=mapping, name=name)
            missing = self.wait_ready(needed)
            if not missing:
                return "（启动卡住，已自动重试）" if attempt == 2 else ""
            LOGGER.warning("lifecycle not active after %.0f s: %s", READY_TIMEOUT_S, missing)
        names = "、".join(MANAGERS.get(m, m) for m in sorted(missing))
        raise RuntimeError(f"导航容器的{names}模块没能启动，请再试一次")

    def wait_ready(self, needed: set[str]) -> set[str]:
        """Managers still not active when the wait ends (empty when all came up)."""
        deadline = self.monotonic() + READY_TIMEOUT_S
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
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    LOGGER.info("serving %s", args.socket)
    try:
        serve(navd, args.socket, stopping)
    finally:
        ctl.cancel()
