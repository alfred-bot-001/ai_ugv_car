"""Browser/device protocol integration and live operation failure paths."""

import json
import struct
import threading
import time
from pathlib import Path

import httpx
import pytest
from anyio import EndOfStream
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tvpet_console.app import create_app
from tvpet_console.config import Settings


@pytest.mark.parametrize("video_protocol", ["console.v1", "console.video.v2"])
def test_online_protocol_control_and_media(tmp_path: Path, video_protocol: str) -> None:
    origin = "https://testserver"
    app = create_app(Settings(tmp_path, origin=origin, face_service_url="http://localhost:8000"))
    app.state.gateway.faces.transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "faces": [{"name": "max", "confidence": 0.8, "bbox": [10, 20, 40, 80]}],
                "time_ms": 64,
            },
        )
    )
    app.state.store.initialize("test-only-long-password")
    token = "test-only-device-token"
    app.state.store.register_agent("laosan", token)
    with TestClient(app, base_url=origin) as client:
        auth = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-only-long-password"},
            headers={"Origin": origin},
        ).json()
        headers = {"Origin": origin, "X-CSRF-Token": auth["csrf"]}
        with client.websocket_connect(
            "/internal/v1/agents/laosan/control",
            headers={"Authorization": "Bearer " + token},
            subprotocols=["agent.v1"],
        ) as device:
            welcome = device.receive_json()
            link = welcome["link_id"]
            device.send_json(
                {"kind": "status", "state": {"mode": "observe", "system": {"temperature_c": 45}}}
            )
            device.send_json({"kind": "challenge", "value": "test-valid-challenge"})
            stop = threading.Event()
            commands = []

            def responder():
                try:
                    while not stop.is_set():
                        command = device.receive_json()
                        if command.get("kind") == "command":
                            commands.append(command)
                            device.send_json(
                                {
                                    "kind": "result",
                                    "id": command["id"],
                                    "ok": True,
                                    "lease": "test-valid-lease",
                                }
                            )
                except (WebSocketDisconnect, RuntimeError, EndOfStream):
                    pass

            thread = threading.Thread(target=responder, daemon=True)
            thread.start()
            for _ in range(20):
                if app.state.gateway.online:
                    break
                time.sleep(0.01)
            assert client.get("/api/v1/status").json()["online"]
            with client.websocket_connect(
                "wss://testserver/ws/control",
                headers={"Origin": origin},
                subprotocols=["console.v1"],
            ) as control:
                control.send_json({"csrf": auth["csrf"]})
                assert control.receive_json()["online"]
                assert client.post("/api/v1/control/lease", headers=headers).status_code == 200
                control.send_json(
                    {
                        "target": "gimbal",
                        "x": 0.2,
                        "y": 0,
                        "lease": "test-valid-lease",
                        "challenge": "test-valid-challenge",
                        "sequence": 1,
                    }
                )
                assert control.receive_json()["ok"]
                control.send_json(
                    {
                        "target": "wheels",
                        "x": 0,
                        "y": 1,
                        "lease": "test-valid-lease",
                        "challenge": "test-valid-challenge",
                        "sequence": 2,
                        "speed": 0.15,
                    }
                )
                reply = control.receive_json()
                while "ok" not in reply:  # challenge frames interleave with results
                    reply = control.receive_json()
                assert reply["ok"]
            for action in ("stop", "estop", "reset"):
                assert client.post("/api/v1/control/" + action, headers=headers).status_code == 200
            assert client.post("/api/v1/control/unknown", headers=headers).status_code == 404
            with client.websocket_connect(
                "/internal/v1/agents/laosan/video",
                headers={"Authorization": "Bearer " + token, "X-Agent-Link": link},
                subprotocols=["agent.v1"],
            ) as video:
                with client.websocket_connect(
                    "wss://testserver/ws/video",
                    headers={"Origin": origin},
                    subprotocols=[video_protocol],
                ) as viewer:
                    viewer.send_json({"csrf": auth["csrf"]})
                    time.sleep(0.04)
                    meta = json.dumps({"sequence": 10}).encode()
                    jpeg = b"\xff\xd8fresh\xff\xd9"
                    video.send_bytes(struct.pack("!I", len(meta)) + meta + jpeg)
                    packet = viewer.receive_bytes()
                    if video_protocol == "console.video.v2":
                        size = struct.unpack_from("!I", packet)[0]
                        info = json.loads(packet[4 : 4 + size])
                        assert info["sequence"] == 10 and info["faces"][0]["label"] == "爸爸"
                        assert packet[4 + size :] == jpeg
                    else:
                        assert packet == jpeg
            assert client.post("/api/v1/speech/transcribe", headers=headers).status_code == 409
            assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
            stop.set()
        thread.join(timeout=0.5)
        sticks = [command["args"] for command in commands if command["action"] == "stick"]
        assert [stick["speed"] for stick in sticks] == [0.1, 0.15]
