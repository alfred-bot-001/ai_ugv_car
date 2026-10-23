"""Map page commands on the Pi: strict validation, ugv-navd forwarding, a local stop first."""

import asyncio
import json
import math
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tvpet_pi.agent import AgentRuntime
from tvpet_pi.nav_ops import NavCommand, NavdClient
from tvpet_pi.navframe import parse_packet


def test_commands_take_exactly_their_fields() -> None:
    assert NavCommand.model_validate({"op": "mapping"}).payload() == {"op": "mapping"}
    save = NavCommand.model_validate({"op": "save", "map": "room-1"})
    assert save.describe() == "保存地图 room-1"
    goal = NavCommand.model_validate({"op": "goal", "x": 1, "y": -0.5, "yaw": math.pi / 2})
    assert goal.payload() == {"op": "goal", "x": 1.0, "y": -0.5, "yaw": math.pi / 2}
    assert goal.describe() == "导航去目标点 (1.00, -0.50, 90°)"
    assert NavCommand.model_validate({"op": "explore"}).describe() == "自动探索"
    rename = NavCommand.model_validate({"op": "rename", "map": "a", "to": "b"})
    assert rename.payload() == {"op": "rename", "map": "a", "to": "b"}
    assert rename.describe() == "地图改名 a → b"
    route = {"loops": 2, "waypoints": [{"x": 1.0, "y": 0.5}]}
    saved = NavCommand.model_validate({"op": "route_save", "map": "a", "route": route})
    assert saved.payload()["route"] == {"loops": 2, "waypoints": [{"x": 1.0, "y": 0.5, "yaw": 0.0}]}
    assert NavCommand.model_validate({"op": "patrol"}).describe() == "开始巡航"
    for bad in (
        {"op": "shell"},
        {"op": "mapping", "map": "x"},
        {"op": "save"},
        {"op": "save", "map": "../x"},
        {"op": "goal", "x": 1, "y": 0},
        {"op": "goal", "x": True, "y": 0, "yaw": 0},
        {"op": "goal", "x": "1", "y": 0, "yaw": 0},
        {"op": "goal", "x": math.nan, "y": 0, "yaw": 0},
        {"op": "goal", "x": math.inf, "y": 0, "yaw": 0},
        {"op": "goal", "x": 0, "y": 0, "yaw": 7},
        {"op": "stop", "x": 0},
        {"op": "stop", "force": True},
        {"op": "explore", "x": 1},
        {"op": "rename", "map": "a"},
        {"op": "rename", "map": "a", "to": "../b"},
        {"op": "delete", "map": "a", "to": "b"},
        {"op": "preview"},
        {"op": "route_save", "map": "a"},
        {"op": "route_save", "map": "a", "route": {"waypoints": []}},
        {"op": "route_save", "map": "a", "route": {"loops": 11, "waypoints": [{"x": 0, "y": 0}]}},
        {"op": "route_save", "map": "a", "route": {"waypoints": [{"x": 0, "y": 0, "z": 1}]}},
        {"op": "route_save", "map": "a", "route": {"waypoints": [{"x": 0, "y": 0}] * 21}},
        {"op": "patrol", "route": {"waypoints": [{"x": 0, "y": 0}]}},
    ):
        with pytest.raises(ValidationError):
            NavCommand.model_validate(bad)


def serve_once(path: Path, reply: bytes, seen: list[dict[str, Any]]) -> threading.Thread:
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(1)

    def answer() -> None:
        with server:
            connection, _ = server.accept()
            with connection:
                seen.append(json.loads(connection.makefile().readline()))
                connection.sendall(reply)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return thread


def test_navd_client_replies_and_errors() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        path = Path(short) / "ops.sock"
        seen: list[dict[str, Any]] = []
        client = NavdClient(path, timeout=1)
        with pytest.raises(ValueError, match="没有响应"):
            client.request({"op": "status"})
        serve_once(path, b'{"ok": true, "job": {"id": "a"}}\n', seen).join(0)
        assert client.request({"op": "mapping"})["job"] == {"id": "a"}
        assert seen == [{"op": "mapping"}]
        for reply, message in (
            (b'{"ok": false, "error": "busy"}\n', "busy"),
            (b"garbage\n", "格式无效"),
            (b"[1]\n", "格式无效"),
        ):
            path.unlink()
            serve_once(path, reply, seen)
            with pytest.raises(ValueError, match=message):
                client.request({"op": "status"})


class FakeNavd:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = replies
        self.requests: list[dict[str, Any]] = []

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(payload)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return dict(reply)


def runtime_with(tmp_path: Path, navd: FakeNavd) -> tuple[AgentRuntime, list[str]]:
    runtime = AgentRuntime({"nav_dir": str(tmp_path / "nav")})
    runtime.navd = navd  # type: ignore[assignment]
    stops: list[str] = []
    runtime.board.stop = lambda emergency=False: stops.append("stop")  # type: ignore[method-assign]
    return runtime, stops


def logs(runtime: AgentRuntime) -> list[str]:
    assert runtime.nav_stream is not None
    return [parse_packet(p)[1]["text"] for p in runtime.nav_stream.cache.logs]


def test_goal_and_cancel_go_through_navd(tmp_path: Path) -> None:
    navd = FakeNavd([{"ok": True, "job": {"id": "g", "state": "running"}}, {"ok": True}])
    runtime, stops = runtime_with(tmp_path, navd)
    reply = asyncio.run(runtime.nav_command(NavCommand(op="goal", x=1.0, y=0.0, yaw=0.0)))
    assert reply["job"]["id"] == "g" and navd.requests[-1]["op"] == "goal"
    assert stops == [] and logs(runtime)[-1] == "网页操作：导航去目标点 (1.00, 0.00, 0°)"
    runtime.navd_status = {"job": {"id": "g", "state": "running"}}
    asyncio.run(runtime.nav_command(NavCommand(op="stop")))
    assert stops == ["stop"] and runtime.nav is not None
    assert runtime.nav.fault is not None and runtime.nav.fault["reason"] == "operator_cancel"
    assert "已停车：网页取消了导航" in logs(runtime)


def test_cancel_stops_even_when_navd_is_down(tmp_path: Path) -> None:
    navd = FakeNavd([ValueError("导航服务 ugv-navd 没有响应"), ValueError("down")])
    runtime, stops = runtime_with(tmp_path, navd)
    reply = asyncio.run(runtime.nav_command(NavCommand(op="stop")))
    assert reply["stopped"] and "没有响应" in reply["navd_error"] and stops == ["stop"]
    assert runtime.nav is not None and runtime.nav.fault is None  # nothing was running
    with pytest.raises(ValueError, match="down"):
        asyncio.run(runtime.nav_command(NavCommand(op="mapping")))


def test_nav_action_and_unconfigured_runtime(tmp_path: Path) -> None:
    class Socket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, message: str) -> None:
            self.sent.append(message)

    navd = FakeNavd([{"ok": True, "maps": [{"name": "room"}]}])
    runtime, _ = runtime_with(tmp_path, navd)
    runtime.control = Socket()  # type: ignore[assignment]
    asyncio.run(runtime.command({"id": "1", "action": "nav", "args": {"op": "maps"}}))
    asyncio.run(runtime.command({"id": "2", "action": "nav", "args": {"op": "rm"}}))
    sent = [json.loads(m) for m in runtime.control.sent]  # type: ignore[union-attr]
    assert sent[0]["ok"] and sent[0]["nav"]["maps"] == [{"name": "room"}]
    assert not sent[1]["ok"]
    with pytest.raises(ValueError, match="not configured"):
        asyncio.run(AgentRuntime({}).nav_command(NavCommand(op="maps")))


def test_finished_jobs_are_logged_once(tmp_path: Path) -> None:
    old = {"id": "old", "op": "save", "state": "ok", "message": "地图已保存为 a"}
    running = {"id": "new", "op": "mapping", "state": "running", "message": ""}
    done = {**running, "state": "failed", "message": "命令失败（退出码 1）"}
    navd = FakeNavd(
        [{"ok": True, "job": j, "container": "running"} for j in (old, old, running, done, done)]
        + [ValueError("导航服务 ugv-navd 没有响应")]
    )
    runtime, _ = runtime_with(tmp_path, navd)
    for _ in range(5):
        status = runtime.read_navd()
    assert status["online"] and "ok" not in status
    assert [t for t in logs(runtime) if "：" in t] == ["开始建图：命令失败（退出码 1）"]
    assert runtime.read_navd() == {"online": False, "error": "导航服务 ugv-navd 没有响应"}
    runtime.navd_status = status
    agent = parse_packet(runtime.nav_agent_packet())[1]
    assert agent["navd"]["job"]["state"] == "failed"
