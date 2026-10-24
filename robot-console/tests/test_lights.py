"""Light switches: two toggles on the live video reach the car as one `lights` command."""

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


def test_light_switches_reach_the_device_with_audit(tmp_path: Path) -> None:
    app = create_app(Settings(tmp_path, origin=ORIGIN))
    store = app.state.store
    store.initialize(PASSWORD)
    store.register_agent("laosan", TOKEN)
    gateway = app.state.gateway
    with TestClient(app, base_url=ORIGIN) as client:
        headers = {"Origin": ORIGIN}
        csrf = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": PASSWORD},
            headers=headers,
        ).json()["csrf"]
        secured = {**headers, "X-CSRF-Token": csrf}
        page = client.get("/").text
        stage = page[page.index('class="video-stage"') : page.index('class="panel-footer"')]
        assert 'id="light-base"' in stage and 'id="light-head"' in stage
        offline = client.post("/api/v1/control/lights", json={"base": True}, headers=secured)
        assert offline.status_code == 409
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
                        reply: dict[str, Any] = {"kind": "result", "id": message["id"], "ok": True}
                        if message["action"] == "lights":
                            reply["lights"] = {"base": True, "head": False}
                        device.send_json(reply)
                except (WebSocketDisconnect, RuntimeError, EndOfStream):
                    pass

            thread = threading.Thread(target=responder, daemon=True)
            thread.start()
            for _ in range(50):
                if gateway.online:
                    break
                time.sleep(0.01)
            body = {"base": True}
            no_csrf = client.post("/api/v1/control/lights", json=body, headers=headers)
            assert no_csrf.status_code == 403
            reply = client.post("/api/v1/control/lights", json=body, headers=secured)
            assert reply.status_code == 200
            assert reply.json()["lights"] == {"base": True, "head": False}
            assert received[-1]["action"] == "lights" and received[-1]["args"] == body
            for bad in ({"base": "on"}, {"IO4": 255}, {"base": True, "head": True, "all": True}):
                rejected = client.post("/api/v1/control/lights", json=bad, headers=secured)
                assert rejected.status_code == 422
            assert [m["action"] for m in received] == ["lights"]
            stop.set()
        with Session(store.engine) as session:
            events = [row.event for row in session.scalars(select(Audit)).all()]
        assert "device.lights" in events
