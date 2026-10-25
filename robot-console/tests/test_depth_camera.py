"""Front depth: device channel, browser feed with distances, lease and cleanup."""

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

ORIGIN = "https://testserver"
PASSWORD = "test-only-long-password"
TOKEN = "test-only-device-token"


def test_depth_frames_reach_the_browser_with_their_distances(tmp_path: Path) -> None:
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
                "/internal/v1/agents/laosan/depth",
                headers={"Authorization": "Bearer " + TOKEN, "X-Agent-Link": link},
                subprotocols=["agent.v1"],
            ) as front:
                with client.websocket_connect(
                    "wss://testserver/ws/depth",
                    headers={"Origin": ORIGIN},
                    subprotocols=["console.v1"],
                ) as viewer:
                    viewer.send_json({"csrf": csrf})
                    time.sleep(0.05)
                    meta = json.dumps(
                        {"sequence": 3, "timestamp_ns": 1, "centre_mm": 1234}
                    ).encode()
                    jpeg = b"\xff\xd8depth\xff\xd9"
                    packet = struct.pack("!I", len(meta)) + meta + jpeg
                    front.send_bytes(packet)
                    assert viewer.receive_bytes() == packet  # distances travel with the image
                    assert gateway.depth == packet and gateway.front == b""
                    time.sleep(0.3)
                    assert any(m.get("kind") == "heartbeat" and m.get("depth") for m in received)
            # TestClient cancels the server handler on close, so the release is checked below.
            commands = [m["args"] for m in received if m.get("action") == "media.set"]
            assert {"channel": "depth", "enabled": True} in commands
            stop.set()
        thread.join(timeout=1)
        assert gateway.depth == b""  # cleared with the device link
