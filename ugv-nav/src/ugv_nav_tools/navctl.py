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
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .board_client import BoardClient, BoardError
from .route import Pose, Route, load_route

ARM_SECONDS = 120.0
NAVIGATE_TIMEOUT = 110.0
MAP_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
ROS_ENV = 'source /opt/ugv_ws/install/setup.bash && exec "$@"'

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Settings:
    deploy: Path = Path("/home/ws/deploy/ugv-nav")
    board: Path = Path("/home/ws/deploy/ai-tv-pet-agent/nav")
    container: str = "ugv-nav"

    @property
    def compose_dir(self) -> Path:
        return self.deploy / "src"

    @property
    def maps(self) -> Path:
        return self.deploy / "maps"

    @property
    def reports(self) -> Path:
        return self.deploy / "reports"


def map_name(value: str) -> str:
    if not MAP_NAME.fullmatch(value):
        raise ValueError("map names use 1-64 letters, digits, _ or -")
    return value


def mode_env(*, mapping: bool, map_file: str = "/maps/map.yaml") -> str:
    """Compose .env selecting SLAM (mapping) or AMCL on a saved map; Nav2 always loaded."""
    flags = {
        "mapping": mapping,
        "localization": not mapping,
        "navigation": True,
    }
    args = " ".join(f"{k}:={str(v).lower()}" for k, v in flags.items()) + f" map:={map_file}"
    return f"UGV_NAV_LAUNCH=n3_navigation\nUGV_NAV_ARGS={args}\n"


def write_arm(board: Path, *, enabled: bool, now: float, seconds: float = ARM_SECONDS) -> str:
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
    temporary = board / ".arm.json.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle)
    os.replace(temporary, board / "arm.json")
    return session


class NavCtl:
    def __init__(
        self,
        settings: Settings,
        run: Runner = subprocess.run,
        board: BoardClient | None = None,
        clock: Callable[[], float] = time.time,
        echo: Callable[[str], None] = print,
    ) -> None:
        self.settings = settings
        self.run = run
        self.board = board or BoardClient(settings.board / "board.sock", timeout=0.5)
        self.clock = clock
        self.echo = echo

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

    @contextlib.contextmanager
    def armed(self) -> Iterator[str]:
        """One bounded motion window; always disarmed and stopped afterwards."""
        self.disarm()
        session = write_arm(self.settings.board, enabled=True, now=self.clock())
        try:
            yield session
        finally:
            self.disarm()

    def status(self) -> dict[str, Any]:
        reply = self.board.status()
        return {k: reply.get(k) for k in ("age", "estopped", "nav_owned", "telemetry")}

    def set_mode(self, *, mapping: bool, name: str | None = None) -> None:
        self.disarm()
        map_file = "/maps/map.yaml"
        if not mapping:
            if name is None or not (self.settings.maps / f"{map_name(name)}.yaml").exists():
                raise ValueError("saved map not found")
            map_file = f"/maps/{name}.yaml"
        (self.settings.compose_dir / ".env").write_text(
            mode_env(mapping=mapping, map_file=map_file)
        )
        self.compose("up", "-d", "--force-recreate")

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

    def goal(self, pose: Pose) -> dict[str, Any]:
        """Plan, then drive to one pose inside one arm window."""
        self.disarm()
        self.json_op("cancel")
        with self.armed():
            return self.json_op(
                "navigate", str(pose.x), str(pose.y), str(pose.yaw),
                "--timeout", str(NAVIGATE_TIMEOUT),
            )  # fmt: skip

    def patrol(self, route: Route) -> dict[str, Any]:
        """Drive the route leg by leg; the first failed leg stops the patrol."""
        legs = []
        for loop, index, pose in route.legs():
            self.echo(f"巡航 第 {loop} 圈 第 {index} 点 → ({pose.x:.2f}, {pose.y:.2f})")
            try:
                result = self.goal(pose)
            except (RuntimeError, BoardError, ValueError) as error:
                result = {"ok": False, "error": str(error)}  # goal() already disarmed
            result |= {"loop": loop, "index": index}
            legs.append(result)
            if not result.get("ok"):
                break
        summary = {
            "ok": len(legs) == len(route.legs()) and all(leg.get("ok") for leg in legs),
            "completed_legs": sum(bool(leg.get("ok")) for leg in legs),
            "planned_legs": len(route.legs()),
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
    return root


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
    else:
        route = load_route(args.route, args.loops)  # validate before any motion
        output = ctl.patrol(route)
    ctl.echo(output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))
    ok = not isinstance(output, dict) or output.get("ok", True) is not False
    return 0 if ok else 2


def entry() -> None:
    sys.exit(main())
