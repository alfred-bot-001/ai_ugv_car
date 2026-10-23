"""Map page commands: validated here, carried out by ugv-navd on the Pi host.

pi-agent's sandbox cannot run docker, so mapping, saving, localizing, setting the pose
and goals go to ugv-navd's private socket (ugv_nav_tools.navd validates them again).
The console validates the same model before sending anything to the car.
"""

import json
import math
import socket
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NAVD_SOCKET = Path("/home/ws/deploy/ugv-nav/run/ops.sock")
COORD_LIMIT = 100.0  # m; same bound as ugv-navd
MAX_REPLY = 262144  # the map list is the largest reply
NavOp = Literal[
    "status",
    "maps",
    "mapping",
    "save",
    "localize",
    "pose",
    "goal",
    "explore",
    "stop",
    "preview",
    "rename",
    "delete",
    "default",
]
MAP_OPS = ("save", "localize", "preview", "rename", "delete", "default")
POSE_OPS = ("pose", "goal")
# What the map page log says when the operator starts something.
OP_TEXT = {
    "mapping": "开始建图",
    "save": "保存地图",
    "localize": "切换到已保存地图定位",
    "pose": "设置当前位置",
    "goal": "导航去目标点",
    "explore": "自动探索",
    "stop": "取消导航",
    "rename": "地图改名",
    "delete": "删除地图（移到回收区）",
    "default": "设为默认地图",
}


class NavCommand(BaseModel):
    """Exactly the fields each operation takes."""

    model_config = ConfigDict(extra="forbid")
    op: NavOp
    map: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    to: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    x: float | None = Field(default=None, strict=True, ge=-COORD_LIMIT, le=COORD_LIMIT)
    y: float | None = Field(default=None, strict=True, ge=-COORD_LIMIT, le=COORD_LIMIT)
    yaw: float | None = Field(default=None, strict=True, ge=-2 * math.pi, le=2 * math.pi)

    @model_validator(mode="after")
    def fields_match_op(self) -> "NavCommand":
        if (self.map is not None) != (self.op in MAP_OPS):
            raise ValueError("a map name goes with the saved-map operations only")
        if (self.to is not None) != (self.op == "rename"):
            raise ValueError("a new name goes with rename only")
        pose = (self.x, self.y, self.yaw)
        if self.op in POSE_OPS:
            if any(v is None or not math.isfinite(v) for v in pose):
                raise ValueError("pose and goal need finite x, y and yaw")
        elif any(v is not None for v in pose):
            raise ValueError("x, y and yaw go with pose and goal only")
        return self

    def payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)

    def describe(self) -> str:
        text = OP_TEXT.get(self.op, self.op)
        if self.to:
            return f"{text} {self.map} → {self.to}"
        if self.map:
            return f"{text} {self.map}"
        if self.op in POSE_OPS:
            return f"{text} ({self.x:.2f}, {self.y:.2f}, {math.degrees(self.yaw or 0):.0f}°)"
        return text


class NavdClient:
    """One newline-delimited JSON request per connection, as ugv-navd serves it."""

    def __init__(self, path: Path = NAVD_SOCKET, timeout: float = 2.0) -> None:
        self.path = path
        self.timeout = timeout

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(self.timeout)
                connection.connect(str(self.path))
                connection.sendall(json.dumps(payload, allow_nan=False).encode() + b"\n")
                data = b""
                while b"\n" not in data and len(data) < MAX_REPLY:
                    part = connection.recv(65536)
                    if not part:
                        break
                    data += part
        except OSError as error:
            raise ValueError("导航服务 ugv-navd 没有响应") from error
        try:
            reply = json.loads(data)
        except ValueError as error:
            raise ValueError("导航服务回复格式无效") from error
        if not isinstance(reply, dict):
            raise ValueError("导航服务回复格式无效")
        if reply.get("ok") is not True:
            raise ValueError(str(reply.get("error") or "导航服务拒绝了操作"))
        return reply
