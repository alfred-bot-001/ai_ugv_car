"""Map page controls: login, CSRF, strict bodies and audit before anything reaches the car."""

import base64
import threading
import time
from pathlib import Path
from typing import Any

from anyio import EndOfStream
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect
from tvpet_console.app import create_app
from tvpet_console.config import Settings
from tvpet_console.store import Audit

ORIGIN = "https://testserver"
PASSWORD = "test-only-long-password"
TOKEN = "test-only-device-token"


def test_nav_commands_reach_the_device_with_audit(tmp_path: Path) -> None:
    app = create_app(Settings(tmp_path, origin=ORIGIN))
    store = app.state.store
    store.initialize(PASSWORD)
    store.register_agent("laosan", TOKEN)
    gateway = app.state.gateway
    with TestClient(app, base_url=ORIGIN) as client:
        headers = {"Origin": ORIGIN}
        anonymous = client.post("/api/v1/nav/command", json={"op": "mapping"}, headers=headers)
        assert anonymous.status_code == 401
        csrf = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": PASSWORD},
            headers=headers,
        ).json()["csrf"]
        with client.websocket_connect(
            "/internal/v1/agents/laosan/control",
            headers={"Authorization": "Bearer " + TOKEN},
            subprotocols=["agent.v1"],
        ) as device:
            device.receive_json()
            device.send_json({"kind": "status", "state": {"mode": "observe"}})
            received: list[dict[str, Any]] = []
            stop = threading.Event()

            def responder() -> None:
                try:
                    while not stop.is_set():
                        message = device.receive_json()
                        if message.get("kind") != "command":
                            continue
                        received.append(message)
                        args = message["args"]
                        reply: dict[str, Any] = {"kind": "result", "id": message["id"], "ok": True}
                        if args.get("op") == "maps":
                            reply["nav"] = {
                                "ok": True,
                                "maps": [{"name": "room"}],
                                "default": "room",
                            }
                        elif args.get("op") == "preview":
                            png = base64.b64encode(b"\x89PNG\r\n\x1a\nthumb").decode()
                            reply["nav"] = {"ok": True, "png": png, "meta": {}}
                        elif args.get("op") == "route":
                            reply["nav"] = {"ok": True, "route": {"loops": 1, "waypoints": []}}
                        elif args.get("op") == "reports":
                            reply["nav"] = {"ok": True, "reports": [{"name": "patrol-1.json"}]}
                        elif args.get("op") == "rename":
                            reply["nav"] = {"ok": True, "renamed": args["to"]}
                        elif args.get("op") == "save":
                            reply.update(ok=False, error="「开始建图」还没完成，请稍候")
                        else:
                            reply["nav"] = {"ok": True, "job": {"id": "j1", "op": args["op"]}}
                        device.send_json(reply)
                except (WebSocketDisconnect, RuntimeError, EndOfStream):
                    pass

            thread = threading.Thread(target=responder, daemon=True)
            thread.start()
            for _ in range(50):
                if gateway.online:
                    break
                time.sleep(0.01)
            secured = {**headers, "X-CSRF-Token": csrf}
            goal = {"op": "goal", "x": 1.5, "y": -0.25, "yaw": 0.5}
            assert client.post("/api/v1/nav/command", json=goal, headers=headers).status_code == 403
            reply = client.post("/api/v1/nav/command", json=goal, headers=secured)
            assert reply.status_code == 200 and reply.json()["job"]["op"] == "goal"
            assert received[-1]["action"] == "nav" and received[-1]["args"] == goal
            for bad in (
                {"op": "goal", "x": 1, "y": 0},
                {"op": "goal", "x": 1, "y": 0, "yaw": 0, "speed": 2},
                {"op": "localize", "map": "../../etc"},
                {"op": "reboot"},
            ):
                response = client.post("/api/v1/nav/command", json=bad, headers=secured)
                assert response.status_code == 422
            assert (
                client.post("/api/v1/nav/command", json={"op": "maps"}, headers=secured).status_code
                == 404
            )
            busy = client.post(
                "/api/v1/nav/command", json={"op": "save", "map": "room"}, headers=secured
            )
            assert busy.status_code == 409 and "还没完成" in busy.json()["detail"]
            assert client.get("/api/v1/nav/maps").json() == {
                "maps": [{"name": "room"}],
                "default": "room",
            }
            thumb = client.get("/api/v1/nav/maps/room/preview.png")
            assert thumb.status_code == 200 and thumb.content.startswith(b"\x89PNG")
            assert thumb.headers["cache-control"] == "no-store"
            assert client.get("/api/v1/nav/maps/a.b/preview.png").status_code == 404
            renamed = client.post(
                "/api/v1/nav/command",
                json={"op": "rename", "map": "room", "to": "study"},
                headers=secured,
            )
            assert renamed.status_code == 200 and renamed.json()["renamed"] == "study"
            preview_post = {"op": "preview", "map": "room"}
            assert (
                client.post("/api/v1/nav/command", json=preview_post, headers=secured).status_code
                == 404
            )
            assert client.get("/api/v1/nav/routes/room").json()["route"]["loops"] == 1
            assert client.get("/api/v1/nav/routes/a.b").status_code == 404
            assert client.get("/api/v1/nav/reports").json() == {
                "reports": [{"name": "patrol-1.json"}]
            }
            route_post = {"op": "route", "map": "room"}
            assert (
                client.post("/api/v1/nav/command", json=route_post, headers=secured).status_code
                == 404
            )
            sent = [m["args"]["op"] for m in received]
            assert sent == ["goal", "save", "maps", "preview", "rename", "route", "reports"]
            stop.set()
        thread.join(timeout=1)
        assert client.get("/api/v1/nav/maps").status_code == 409  # offline
    with Session(store.engine) as db:
        events = [row.event for row in db.scalars(select(Audit))]
    assert "nav.goal" in events and "nav.rename" in events and "nav.save" not in events
