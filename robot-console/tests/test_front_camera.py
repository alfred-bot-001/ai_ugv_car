"""Front camera: device channel, browser feed, lease command and cleanup."""

import asyncio
import json
import struct
import threading
import time
from pathlib import Path
from typing import Any

from anyio import EndOfStream
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tvpet_console.app import create_app
from tvpet_console.config import Settings
from tvpet_console.gateway import Gateway

ORIGIN = "https://testserver"
PASSWORD = "test-only-long-password"
TOKEN = "test-only-device-token"


def test_front_camera_frames_reach_the_browser(tmp_path: Path) -> None:
    app = create_app(Settings(tmp_path, origin=ORIGIN))
    app.state.store.initialize(PASSWORD)
    app.state.store.register_agent("laosan", TOKEN)
    gateway = app.state.gateway
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": PASSWORD},
            headers={"Origin": ORIGIN},
        ).json()["csrf"]
        with client.websocket_connect(
            "/internal/v1/agents/laosan/control",
            headers={"Authorization": "Bearer " + TOKEN},
            subprotocols=["agent.v1"],
        ) as device:
            link = device.receive_json()["link_id"]
            device.send_json({"kind": "status", "state": {"mode": "observe"}})
            received: list[dict[str, Any]] = []
            stop = threading.Event()

            def responder() -> None:
                try:
                    while not stop.is_set():
                        message = device.receive_json()
                        received.append(message)
                        if message.get("kind") == "command":
                            device.send_json({"kind": "result", "id": message["id"], "ok": True})
                except (WebSocketDisconnect, RuntimeError, EndOfStream):
                    pass

            thread = threading.Thread(target=responder, daemon=True)
            thread.start()
            for _ in range(50):
                if gateway.online:
                    break
                time.sleep(0.01)
            with client.websocket_connect(
                "/internal/v1/agents/laosan/front",
                headers={"Authorization": "Bearer " + TOKEN, "X-Agent-Link": link},
                subprotocols=["agent.v1"],
            ) as front:
                with client.websocket_connect(
                    "wss://testserver/ws/front",
                    headers={"Origin": ORIGIN},
                    subprotocols=["console.v1"],
                ) as viewer:
                    viewer.send_json({"csrf": csrf})
                    time.sleep(0.05)
                    meta = json.dumps({"sequence": 3, "timestamp_ns": 1}).encode()
                    jpeg = b"\xff\xd8front\xff\xd9"
                    front.send_bytes(struct.pack("!I", len(meta)) + meta + jpeg)
                    assert viewer.receive_bytes() == jpeg
                    assert gateway.front == jpeg and gateway.video == b""  # never the gimbal feed
                    time.sleep(0.3)
                    assert any(m.get("kind") == "heartbeat" and m.get("front") for m in received)
            # TestClient cancels the server handler on close, so the release is checked below.
            commands = [m["args"] for m in received if m.get("action") == "media.set"]
            assert {"channel": "front", "enabled": True} in commands
            stop.set()
        thread.join(timeout=1)
        assert gateway.front == b""  # cleared with the device link


class Control:
    """Device control link that acknowledges every command."""

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []

    async def receive_json(self) -> Any:
        return await self.inbox.get()

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if message.get("kind") == "command":
            await self.inbox.put({"kind": "result", "id": message["id"], "ok": True})

    async def close(self, **kwargs: Any) -> None:
        pass


def test_front_viewers_open_and_release_the_camera() -> None:
    async def scenario() -> None:
        gateway = Gateway()
        control = Control(gateway)
        loop = asyncio.create_task(gateway.control_loop(control))
        await control.inbox.put({"kind": "status", "state": {}})
        await asyncio.sleep(0.01)
        await gateway.viewer("front", 1)
        await gateway.viewer("front", 1)
        await gateway.viewer("front", -1)
        await gateway.viewer("front", -1)
        sets = [m["args"] for m in control.sent if m.get("action") == "media.set"]
        assert sets[0] == {"channel": "front", "enabled": True}
        assert sets[-1] == {"channel": "front", "enabled": False}
        await gateway.revoke()
        assert {"channel": "front", "enabled": False} in [
            m["args"] for m in control.sent if m.get("action") == "media.set"
        ][len(sets) :]
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)

    asyncio.run(scenario())
