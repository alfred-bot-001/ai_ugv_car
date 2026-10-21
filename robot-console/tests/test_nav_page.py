"""Map page: the device nav channel, the browser feed and the page itself."""

import asyncio
import json
import struct
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from anyio import EndOfStream
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tvpet_console.app import create_app
from tvpet_console.config import Settings
from tvpet_console.gateway import Gateway
from tvpet_pi.navframe import PNG_SIGNATURE, parse_packet

ORIGIN = "https://testserver"
PASSWORD = "test-only-long-password"
TOKEN = "test-only-device-token"


def raw(meta: dict[str, Any], payload: bytes = b"") -> bytes:
    body = json.dumps(meta).encode()
    return struct.pack("!I", len(body)) + body + payload


class Socket:
    def __init__(self) -> None:
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[Any] = []

    async def receive_json(self) -> Any:
        return await self.inbox.get()

    async def receive_bytes(self) -> bytes:
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self, **kwargs: Any) -> None:
        pass


def test_gateway_caches_the_feed_and_counts_viewers_without_commands() -> None:
    async def scenario() -> None:
        gateway = Gateway()
        control = Socket()
        loop = asyncio.create_task(gateway.control_loop(control))
        await control.inbox.put({"kind": "status", "state": {}})
        await asyncio.sleep(0.01)
        await gateway.viewer("nav", 1)
        assert gateway.viewer_counts["nav"] == 1
        assert not any(m.get("kind") == "command" for m in control.sent)  # no RPC for nav
        await asyncio.sleep(0.25)
        assert any(m.get("kind") == "heartbeat" and m.get("nav") for m in control.sent)
        nav = Socket()
        feed = asyncio.create_task(gateway.nav_loop(nav))
        await nav.inbox.put(raw({"kind": "map"}, PNG_SIGNATURE))
        await nav.inbox.put(raw({"kind": "pose", "x": 1}))
        await asyncio.sleep(0.01)
        assert [parse_packet(p)[0] for p in gateway.nav.snapshot()[1]] == ["map", "pose"]
        with pytest.raises(ValueError):
            await gateway.nav_loop(Socket())  # one device feed at a time
        await nav.inbox.put(raw({"kind": "teleport"}))
        with pytest.raises(ValueError):
            await feed
        assert "nav" not in gateway.channels and gateway.nav.snapshot()[1] == []
        await gateway.viewer("nav", -1)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        with pytest.raises(ValueError):
            await gateway.viewer("nav", 1)

    asyncio.run(scenario())


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(Settings(tmp_path, origin=ORIGIN))
    app.state.store.initialize(PASSWORD)
    app.state.store.register_agent("laosan", TOKEN)
    with TestClient(app, base_url=ORIGIN) as test_client:
        yield test_client


def login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    return str(response.json()["csrf"])


def test_page_and_feed_need_a_login(client: TestClient) -> None:
    assert "登录" in client.get("/navigation").text
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/nav", subprotocols=["console.v1"]),
    ):
        pass
    login(client)
    page = client.get("/navigation").text
    assert "激光雷达地图" in page and "/static/navigation.js" in page
    assert '<a href="/navigation">' in client.get("/").text


def test_browser_gets_a_snapshot_then_live_packets(client: TestClient) -> None:
    csrf = login(client)
    with client.websocket_connect(
        "/internal/v1/agents/laosan/control",
        headers={"Authorization": "Bearer " + TOKEN},
        subprotocols=["agent.v1"],
    ) as device:
        link = device.receive_json()["link_id"]
        device.send_json({"kind": "status", "state": {"mode": "observe"}})
        heartbeats: list[dict[str, Any]] = []
        stop = threading.Event()

        def reader() -> None:
            try:
                while not stop.is_set():
                    heartbeats.append(device.receive_json())
            except (WebSocketDisconnect, RuntimeError, EndOfStream):
                pass

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        for _ in range(50):
            if client.get("/api/v1/status").json()["online"]:
                break
            time.sleep(0.01)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                "/internal/v1/agents/laosan/nav",
                headers={"Authorization": "Bearer " + TOKEN, "X-Agent-Link": "wrong"},
                subprotocols=["agent.v1"],
            ),
        ):
            pass
        with client.websocket_connect(
            "/internal/v1/agents/laosan/nav",
            headers={"Authorization": "Bearer " + TOKEN, "X-Agent-Link": link},
            subprotocols=["agent.v1"],
        ) as feed:
            feed.send_bytes(raw({"kind": "map", "width": 1}, PNG_SIGNATURE + b"img"))
            feed.send_bytes(raw({"kind": "log", "text": "Activating amcl"}))
            time.sleep(0.05)
            with client.websocket_connect(
                "wss://testserver/ws/nav", headers={"Origin": ORIGIN}, subprotocols=["console.v1"]
            ) as viewer:
                viewer.send_json({"csrf": csrf})
                first = viewer.receive_bytes()
                assert parse_packet(first)[0] == "map" and first.endswith(b"img")
                assert parse_packet(viewer.receive_bytes())[1]["text"] == "Activating amcl"
                feed.send_bytes(raw({"kind": "pose", "x": 0.5, "y": 0, "yaw": 0}))
                assert parse_packet(viewer.receive_bytes())[1]["x"] == 0.5
                time.sleep(0.3)
                assert any(m.get("nav") is True for m in heartbeats)
        stop.set()
    thread.join(timeout=1)
