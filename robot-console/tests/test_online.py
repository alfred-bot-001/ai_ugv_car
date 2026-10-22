"""Browser/device protocol integration and live operation failure paths."""

import json
import struct
import threading
import time
from pathlib import Path
from typing import Any

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
                            args = command.get("args") or {}
                            if command["action"] == "stick" and args.get("sequence") == 3:
                                # The car no longer holds that lease: code, not a toast.
                                reply = {"ok": False, "error": "控制租约已更换", "code": "lease"}
                            else:
                                reply = {"ok": True, "lease": "test-valid-lease"}
                            device.send_json({"kind": "result", "id": command["id"], **reply})
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

                def reply_for(key: str) -> dict[str, Any]:
                    reply = control.receive_json()
                    while key not in reply:  # online pulses interleave with results
                        reply = control.receive_json()
                    return reply

                # Before a lease the relay answers with the lease code, never a toast.
                control.send_json(
                    {
                        "target": "gimbal",
                        "x": 0.2,
                        "y": 0,
                        "lease": "no-lease-yet-0000",
                        "sequence": 1,
                    }
                )
                assert reply_for("code")["code"] == "lease"
                assert client.post("/api/v1/control/lease", headers=headers).status_code == 200
                control.send_json(
                    {
                        "target": "gimbal",
                        "x": 0.2,
                        "y": 0,
                        "lease": "test-valid-lease",
                        "sequence": 1,
                    }
                )
                assert reply_for("ok") == {"ok": True, "sequence": 1}
                control.send_json(
                    {
                        "target": "wheels",
                        "x": 0,
                        "y": 1,
                        "lease": "test-valid-lease",
                        "sequence": 2,
                        "speed": 0.15,
                    }
                )
                assert reply_for("ok")["ok"]
                control.send_json(
                    {"target": "wheels", "x": 0, "y": 1, "lease": "test-valid-lease", "sequence": 3}
                )
                refused = reply_for("code")
                assert refused["code"] == "lease" and "租约" in refused["error"]
                control.send_json({"target": "wheels", "x": 0, "y": 1, "sequence": 4})
                assert "error" in reply_for("error")  # malformed: no lease field
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
        assert [stick["speed"] for stick in sticks] == [0.1, 0.15, 0.1]
        assert all("challenge" not in stick for stick in sticks)
