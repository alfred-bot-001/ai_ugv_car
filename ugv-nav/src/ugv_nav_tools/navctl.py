"""ugv-navctl: operator commands on the Pi host. The only writer of pi-agent's arm file.

Nothing here moves the car at startup. `goal` and `patrol` open one bounded arm window
per waypoint (at most 120 s), and always disarm and stop afterwards, even on errors.
Everything else (mapping, save, localize, pose) never arms motion.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import places as placefile
from .board_client import BoardClient, BoardError
from .map_files import load_grid, write_keepout_mask
from .places import Place, Places, Room, Zone
from .route import Pose, Route, load_route

ARM_SECONDS = 120.0
NAVIGATE_TIMEOUT = 110.0
MAP_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
ROS_ENV = 'source /opt/ugv_ws/install/setup.bash && exec "$@"'
# S0 §3.4 battery gates on the 3S pack, judged only from standstill voltage. They
# follow the console's estimated bars (2026-10-21, user-approved): below 2 bars
# (10.0 V) no new task starts; below 1 bar (9.5 V) a patrol ends and only goes home;
# below 9.2 V the car stays put. Initial values until a measured discharge curve.
BATTERY_START_V = 10.0
BATTERY_LEG_V = 9.5
BATTERY_CRITICAL_V = 9.2
BATTERY_SAMPLE_S = 3.0
BATTERY_MIN_SAMPLES = 5
STANDSTILL_MPS = 0.01
ENV_MAP = re.compile(r"map:=/maps/([A-Za-z0-9_-]{1,64})\.yaml")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Settings:
    deploy: Path = Path("/home/ws/deploy/ugv-nav")
    board: Path = Path("/home/ws/deploy/ai-tv-pet-agent/nav")
    # Saved maps and patrol reports are household data, kept apart from the deployed
    # application (2026-10-22, user request), like the Mini's workspace-agent/agent-data.
    data: Path = Path("/home/ws/agent-data/ai-ugv-car")
    container: str = "ugv-nav"

    @property
    def compose_dir(self) -> Path:
        return self.deploy / "src"

    @property
    def maps(self) -> Path:
        return self.data / "maps"

    @property
    def reports(self) -> Path:
        return self.data / "reports"


def map_name(value: str) -> str:
    if not MAP_NAME.fullmatch(value):
        raise ValueError("map names use 1-64 letters, digits, _ or -")
    return value


def mode_env(*, mapping: bool, map_file: str = "/maps/map.yaml", keepout: str = "") -> str:
    """Compose .env selecting SLAM (mapping) or AMCL on a saved map; Nav2 always loaded."""
    flags = {
        "mapping": mapping,
        "localization": not mapping,
        "navigation": True,
    }
    args = " ".join(f"{k}:={str(v).lower()}" for k, v in flags.items()) + f" map:={map_file}"
    if keepout:
        args += f" keepout:={keepout}"
    return f"UGV_NAV_LAUNCH=n3_navigation\nUGV_NAV_ARGS={args}\n"


def write_arm(
    board: Path, *, enabled: bool, now: float, seconds: float = ARM_SECONDS, mode: str = ""
) -> str:
    """Atomically write a 0600 arm file; returns the session id ('' when disarming)."""
    session = str(uuid.uuid4()) if enabled else ""
    value: dict[str, Any] = {"enabled": False}
    if enabled:
        if not 0 < seconds <= ARM_SECONDS:
            raise ValueError(f"arm window must be 0..{ARM_SECONDS:.0f} s")
        value = {
            "enabled": True,
            "id": session,
            "expires_at": now + seconds,
            "calibrated": True,
            "units_verified": True,
        }
        if mode:
            value["mode"] = mode
    temporary = board / ".arm.json.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle)
    os.replace(temporary, board / "arm.json")
    return session


class Cancelled(RuntimeError):
    """An operator cancel (ugv-navd stop) came before this motion window opened."""


class BatteryLow(ValueError):
    """Standstill voltage below a gate; `critical` means do not even go home."""

    def __init__(self, volts: float, gate: float, *, critical: bool) -> None:
        super().__init__(f"battery {volts:.2f} V below {gate:.1f} V")
        self.volts = volts
        self.critical = critical


class NavCtl:
    def __init__(
        self,
        settings: Settings,
        run: Runner = subprocess.run,
        board: BoardClient | None = None,
        clock: Callable[[], float] = time.time,
        echo: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.run = run
        self.board = board or BoardClient(settings.board / "board.sock", timeout=0.5)
        self.clock = clock
        self.echo = echo
        self.sleep = sleep
        # ugv-navd cancels from another thread: it sets `cancelled` and disarms under
        # `arm_lock`, so a goal that has not armed yet can no longer open its window.
        self.arm_lock = threading.Lock()
        self.cancelled: Callable[[], bool] = lambda: False

    def standstill_voltage(self) -> float:
        """Median pack voltage while both wheels are still; motor load sags it."""
        samples = []
        for _ in range(int(BATTERY_SAMPLE_S / 0.2)):
            telemetry = self.board.status().get("telemetry") or {}
            values = [telemetry.get(k) for k in ("L", "R", "v")]
            if all(isinstance(v, (int, float)) for v in values):
                left, right, volts = (float(v) for v in values)  # type: ignore[arg-type]
                if abs(left) < STANDSTILL_MPS and abs(right) < STANDSTILL_MPS and volts > 0:
                    samples.append(volts)
            if len(samples) >= BATTERY_MIN_SAMPLES:
                break
            self.sleep(0.2)
        if len(samples) < BATTERY_MIN_SAMPLES:
            raise ValueError("no steady standstill voltage; is the car still and online?")
        return float(statistics.median(samples))

    def check_battery(self, gate: float) -> float:
        volts = self.standstill_voltage()
        if volts < BATTERY_CRITICAL_V:
            raise BatteryLow(volts, BATTERY_CRITICAL_V, critical=True)
        if volts < gate:
            raise BatteryLow(volts, gate, critical=False)
        return volts

    def ros(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = ["sudo", "-n", "docker", "exec", self.settings.container]
        command += ["bash", "-c", ROS_ENV, "ros-op", *args]
        return self.run(command, check=check, capture_output=True, text=True)

    def compose(self, *args: str) -> None:
        command = ["sudo", "-n", "docker", "compose", *args]
        self.run(command, check=True, cwd=self.settings.compose_dir)

    def disarm(self) -> None:
        write_arm(self.settings.board, enabled=False, now=self.clock())
        with contextlib.suppress(BoardError):
            self.board.stop()

    def cancel(self) -> None:
        """Close any motion window now and keep this run's goals from opening one."""
        with self.arm_lock:
            self.disarm()

    @contextlib.contextmanager
    def armed(self) -> Iterator[str]:
        """One bounded motion window; always disarmed and stopped afterwards."""
        with self.arm_lock:
            if self.cancelled():
                raise Cancelled("cancelled before motion")
            self.disarm()
            session = write_arm(self.settings.board, enabled=True, now=self.clock())
        try:
            yield session
        finally:
            self.disarm()

    def status(self) -> dict[str, Any]:
        reply = self.board.status()
        return {k: reply.get(k) for k in ("age", "estopped", "nav_owned", "fault", "telemetry")}

    def set_mode(self, *, mapping: bool, name: str | None = None) -> None:
        self.disarm()
        map_file, keepout = "/maps/map.yaml", ""
        if not mapping:
            if name is None or not (self.settings.maps / f"{map_name(name)}.yaml").exists():
                raise ValueError("saved map not found")
            map_file = f"/maps/{name}.yaml"
            if (self.settings.maps / f"{name}.keepout.yaml").exists():
                keepout = f"/maps/{name}.keepout.yaml"
        (self.settings.compose_dir / ".env").write_text(
            mode_env(mapping=mapping, map_file=map_file, keepout=keepout)
        )
        self.ensure_data()
        self.compose("up", "-d", "--force-recreate")

    def ensure_data(self) -> None:
        """Private data folders; docker would create missing bind mounts as root."""
        for folder in (self.settings.data, self.settings.maps, self.settings.reports):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            folder.chmod(0o700)

    def save(self, name: str) -> None:
        self.ros(
            "ros2", "run", "nav2_map_server", "map_saver_cli", "-f", f"/maps/{map_name(name)}",
            "--ros-args", "-p", "save_map_timeout:=10.0",
            "-p", "free_thresh_default:=0.196", "-p", "occupied_thresh_default:=0.65",
        )  # fmt: skip

    def json_op(self, *args: str) -> dict[str, Any]:
        done = self.ros("ugv-ros-op", *args, check=False)
        lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
        if not lines:
            raise RuntimeError(f"ugv-ros-op {args[0]} failed: {done.stderr.strip()[-400:]}")
        result: dict[str, Any] = json.loads(lines[-1])
        return result

    def stop(self) -> None:
        self.disarm()
        self.json_op("cancel")

    def goal(self, pose: Pose, battery_gate: float = BATTERY_START_V) -> dict[str, Any]:
        """Plan, then drive to one pose inside one arm window; low battery refuses to arm."""
        self.disarm()
        self.json_op("cancel")
        self.check_battery(battery_gate)
        with self.armed():
            return self.json_op(
                "navigate", str(pose.x), str(pose.y), str(pose.yaw),
                "--timeout", str(NAVIGATE_TIMEOUT),
            )  # fmt: skip

    def current_map(self) -> str:
        """The saved map the container is localized on; mapping mode has none."""
        env = (self.settings.compose_dir / ".env").read_text()
        match = ENV_MAP.search(env)
        if "localization:=true" not in env or not match:
            raise ValueError("not localized on a saved map; run ugv-navctl localize MAP first")
        return match.group(1)

    def goto(self, place_id: str) -> dict[str, Any]:
        """Navigate to a named place of the current map (S1 §4.2); targets come only from it."""
        name = self.current_map()
        place = placefile.load(self.settings.maps, name).place(place_id)
        result = self.goal(Pose(place.x, place.y, place.yaw))
        return result | {"place": place.id, "map": name}

    # Places file editing (S1 §4.1). None of these arm or move the car.
    def places(self, name: str) -> Places:
        return placefile.load_or_new(self.settings.maps, map_name(name))

    def save_places(self, places: Places) -> dict[str, Any]:
        path = placefile.save(self.settings.maps, places)
        return {"ok": True, "file": path.name, "places": len(places.places)}

    def place_room(self, name: str, room: Room) -> dict[str, Any]:
        current = self.places(name)
        rooms = tuple(r for r in current.rooms if r.id != room.id) + (room,)
        return self.save_places(replace(current, rooms=rooms))

    def place_add(self, name: str, place: Place) -> dict[str, Any]:
        placefile.check_place(place, load_grid(self.settings.maps / f"{map_name(name)}.yaml"))
        current = self.places(name)
        places = tuple(p for p in current.places if p.id != place.id) + (place,)
        return self.save_places(replace(current, places=places))

    def zone_add(self, name: str, zone: Zone) -> dict[str, Any]:
        current = self.places(name)
        zones = tuple(z for z in current.zones if z.id != zone.id) + (zone,)
        return self.save_places(replace(current, zones=zones))

    def place_rm(self, name: str, item_id: str) -> dict[str, Any]:
        current = self.places(name)
        if any(p.room == item_id for p in current.places):
            raise ValueError(f"room {item_id} still has places; remove them first")
        updated = replace(
            current,
            rooms=tuple(r for r in current.rooms if r.id != item_id),
            places=tuple(p for p in current.places if p.id != item_id),
            zones=tuple(z for z in current.zones if z.id != item_id),
        )
        if updated == current:
            raise ValueError(f"no room, place or zone {item_id}")
        return self.save_places(updated)

    def zones_apply(self, name: str) -> dict[str, Any]:
        """Write <map>.keepout.pgm/.yaml for Nav2's keepout filter (S0 §3.3)."""
        current = placefile.load(self.settings.maps, map_name(name))
        mask, cells = write_keepout_mask(
            self.settings.maps / f"{name}.yaml", [z.polygon for z in current.zones]
        )
        return {"ok": True, "mask": mask.name, "keepout_cells": cells, "zones": len(current.zones)}

    def probe(self, command: str, a: float, b: float, seconds: float) -> dict[str, Any]:
        """One bounded factory-command step with encoder traces (moves the car briefly)."""
        self.disarm()
        self.check_battery(BATTERY_START_V)
        write_arm(
            self.settings.board, enabled=True, now=self.clock(), seconds=seconds + 5, mode="probe"
        )
        try:
            client = BoardClient(self.settings.board / "board.sock", timeout=seconds + 3)
            reply = client.request(
                {"op": "probe", "command": command, "a": a, "b": b, "seconds": seconds}
            )
        finally:
            self.disarm()
        return summarize_probe(reply)

    def home_pose(self) -> Pose | None:
        """The home place of the localized map, if one is marked."""
        try:
            home = placefile.load(self.settings.maps, self.current_map()).home()
        except (OSError, ValueError):
            return None
        return Pose(home.x, home.y, home.yaw) if home else None

    def patrol(self, route: Route) -> dict[str, Any]:
        """Drive the route leg by leg; the first failed leg stops the patrol."""
        legs: list[dict[str, Any]] = []
        abort_reason = ""
        for loop, index, pose in route.legs():
            self.echo(f"巡航 第 {loop} 圈 第 {index} 点 → ({pose.x:.2f}, {pose.y:.2f})")
            gate = BATTERY_START_V if not legs else BATTERY_LEG_V
            try:
                result = self.goal(pose, battery_gate=gate)
            except BatteryLow as low:
                result = {"ok": False, "error": str(low), "battery_v": low.volts}
                abort_reason = "battery"
                home = None if low.critical or not legs else self.home_pose()
                if home is not None:
                    try:
                        result["home"] = self.goal(home, battery_gate=BATTERY_CRITICAL_V)
                    except (RuntimeError, BoardError, ValueError) as error:
                        result["home"] = {"ok": False, "error": str(error)}
            except (RuntimeError, BoardError, ValueError) as error:
                result = {"ok": False, "error": str(error)}  # goal() already disarmed
            result |= {"loop": loop, "index": index}
            legs.append(result)
            if not result.get("ok"):
                abort_reason = abort_reason or str(result.get("fault", {}).get("reason", "failed"))
                break
        summary = {
            "ok": len(legs) == len(route.legs()) and all(leg.get("ok") for leg in legs),
            "completed_legs": sum(bool(leg.get("ok")) for leg in legs),
            "planned_legs": len(route.legs()),
            "abort_reason": abort_reason or None,
            "legs": legs,
        }
        self.settings.reports.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (self.settings.reports / f"patrol-{stamp}.json").write_text(json.dumps(summary, indent=1))
        return summary


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ugv-navctl", description="老三 建图、导航与巡航")
    sub = root.add_subparsers(dest="op", required=True)
    sub.add_parser("status", help="底盘遥测与导航占用")
    sub.add_parser("stop", help="撤销授权、停车并取消导航")
    sub.add_parser("mapping", help="切换到建图模式（不会自行运动）")
    sub.add_parser("where", help="当前地图坐标")
    for name, text in (("save", "保存当前地图"), ("localize", "在已保存的地图上定位")):
        sub.add_parser(name, help=text).add_argument("map", type=map_name)
    for name, text in (("pose", "设置初始位姿"), ("goal", "导航到目标（会运动）")):
        command = sub.add_parser(name, help=text)
        for axis in ("x", "y", "yaw"):
            command.add_argument(axis, type=float)
    patrol = sub.add_parser("patrol", help="按航点巡航（会运动）")
    patrol.add_argument("route", type=Path)
    patrol.add_argument("--loops", type=int)
    sub.add_parser("goto", help="导航到当前地图上的地点（会运动）").add_argument("place")
    probe = sub.add_parser("probe", help="厂家指令单步运动测试，记录左右轮速（会运动）")
    probe.add_argument("command", choices=("t13", "t1"))
    probe.add_argument("a", type=float, help="T13 的 X（m/s）或 T1 的 L")
    probe.add_argument("b", type=float, help="T13 的 Z（rad/s）或 T1 的 R")
    probe.add_argument("--seconds", type=float, default=2.0)

    place = sub.add_parser("place", help="标注房间、地点和禁行区（不会运动）")
    place.add_argument("map", type=map_name)
    actions = place.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="列出房间、地点和禁行区")
    room = actions.add_parser("room", help="添加或更新房间")
    room.add_argument("id")
    room.add_argument("name")
    room.add_argument("--alias", action="append", default=[])
    add = actions.add_parser("add", help="添加或更新地点")
    add.add_argument("id")
    add.add_argument("name")
    add.add_argument("--kind", choices=placefile.PLACE_KINDS, default="observe")
    add.add_argument("--room")
    add.add_argument("--alias", action="append", default=[])
    add.add_argument("--scan", help="云台扫视角度（度），写成 --scan=-90,-45,0,45,90")
    where = add.add_mutually_exclusive_group(required=True)
    where.add_argument("--here", action="store_true", help="用车当前的定位位姿")
    where.add_argument("--at", nargs=3, type=float, metavar=("X", "Y", "YAW"))
    zone = actions.add_parser("zone", help="添加或更新禁行区（多边形顶点 X,Y）")
    zone.add_argument("id")
    zone.add_argument("name")
    zone.add_argument("vertices", nargs="+")
    actions.add_parser("rm", help="删除房间、地点或禁行区").add_argument("id")
    actions.add_parser("apply-zones", help="由禁行区生成 Nav2 掩码")
    return root


def summarize_probe(reply: dict[str, Any]) -> dict[str, Any]:
    """Mean and range of each side over the second half of the step."""
    samples = reply.get("samples") or []
    tail = [s for s in samples if s["t"] >= (samples[-1]["t"] / 2 if samples else 0)]
    summary: dict[str, Any] = {
        "ok": reply.get("ok"),
        "error": reply.get("error"),
        "frame": reply.get("frame"),
        "targets": reply.get("targets"),
        "samples": len(samples),
    }
    for side in ("L", "R"):
        values = [float(s[side]) for s in tail]
        if values:
            summary[f"{side}_mean"] = round(sum(values) / len(values), 4)
            summary[f"{side}_range"] = [round(min(values), 4), round(max(values), 4)]
            summary[f"{side}_zero_share"] = round(
                sum(abs(v) < 0.005 for v in values) / len(values), 2
            )
    summary["odometry"] = reply.get("odometry")
    return summary


def vertex(text: str) -> list[float]:
    x, _, y = text.partition(",")
    return [float(x), float(y)]


def place_command(ctl: NavCtl, args: argparse.Namespace) -> Any:
    name = args.map
    if args.action == "list":
        return ctl.places(name).to_json()
    if args.action == "room":
        raw = {"id": args.id, "name": args.name, "aliases": args.alias}
        room = placefile.parse(
            {"version": 1, "map": name, "map_sha256": "0" * 64, "rooms": [raw]}
        ).rooms[0]
        return ctl.place_room(name, room)
    if args.action == "add":
        if args.here:
            pose = ctl.json_op("pose")
            x, y, yaw = pose["x"], pose["y"], pose["yaw"]
        else:
            x, y, yaw = args.at
        raw = {
            "id": args.id,
            "name": args.name,
            "kind": args.kind,
            "aliases": args.alias,
            "pose": {"x": x, "y": y, "yaw": yaw},
            "scan": [int(a) for a in args.scan.split(",")] if args.scan else [],
        }
        if args.room:
            raw["room"] = args.room
        current = ctl.places(name)
        check = current.to_json()
        check["places"] = [p for p in check["places"] if p["id"] != args.id] + [raw]
        parsed = placefile.parse(check)  # validates the room reference and name clashes
        return ctl.place_add(name, parsed.place(args.id))
    if args.action == "zone":
        raw_zone = {"id": args.id, "name": args.name, "polygon": [vertex(v) for v in args.vertices]}
        zone = placefile.parse(
            {"version": 1, "map": name, "map_sha256": "0" * 64, "zones": [raw_zone]}
        ).zones[0]
        return ctl.zone_add(name, zone)
    if args.action == "rm":
        return ctl.place_rm(name, args.id)
    return ctl.zones_apply(name)


def main(argv: Sequence[str] | None = None, ctl: NavCtl | None = None) -> int:
    args = parser().parse_args(argv)
    ctl = ctl or NavCtl(Settings())
    output: Any = None
    if args.op == "status":
        output = ctl.status()
    elif args.op == "stop":
        ctl.stop()
        output = "已撤销授权、停车并取消导航。"
    elif args.op == "mapping":
        ctl.set_mode(mapping=True)
        output = "已进入建图模式。用主控台手动低速驾驶覆盖房间，然后 ugv-navctl save NAME。"
    elif args.op == "localize":
        ctl.set_mode(mapping=False, name=args.map)
        output = f"已在地图 {args.map} 上定位。先用 ugv-navctl pose X Y YAW 设置初始位姿。"
    elif args.op == "save":
        ctl.save(args.map)
        output = f"地图已保存到 maps/{args.map}.yaml"
    elif args.op == "where":
        output = ctl.json_op("pose")
    elif args.op == "pose":
        output = ctl.json_op("set-pose", str(args.x), str(args.y), str(args.yaw))
    elif args.op == "goal":
        output = ctl.goal(Pose(args.x, args.y, args.yaw))
    elif args.op == "goto":
        output = ctl.goto(args.place)
    elif args.op == "probe":
        output = ctl.probe(args.command, args.a, args.b, args.seconds)
    elif args.op == "place":
        output = place_command(ctl, args)
    else:
        route = load_route(args.route, args.loops)  # validate before any motion
        output = ctl.patrol(route)
    ctl.echo(output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))
    ok = not isinstance(output, dict) or output.get("ok", True) is not False
    return 0 if ok else 2


def entry() -> None:
    sys.exit(main())
