"""Authentication, CSRF, revocation, private storage and offline behavior."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect
from tvpet_console.app import COOKIE, create_app
from tvpet_console.config import Settings
from tvpet_console.store import Admin, BrowserSession, Store

PASSWORD = "test-only-long-password"
ORIGIN = "https://testserver"


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(Settings(tmp_path, origin=ORIGIN))
    app.state.store.initialize(PASSWORD)
    with TestClient(app, base_url=ORIGIN) as test_client:
        yield test_client


def login(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("httponly", "secure", "samesite=strict"))
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf"]}


def test_login_protection_and_offline_page(client: TestClient) -> None:
    assert client.get("/api/v1/status").status_code == 401
    assert "欢迎回来" in client.get("/").text
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
        ).status_code
        == 403
    )
    headers = login(client)
    assert "摄像头云台" in client.get("/").text
    assert 'id="stick-gimbal"' in client.get("/").text
    assert client.get("/api/v1/status").json()["online"] is False
    assert client.post("/api/v1/control/stop", headers=headers).status_code == 409
    assert client.post("/api/v1/snapshot", headers=headers).status_code == 409
    assert client.get("/static/style.css").status_code == 200
    assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]


def test_csrf_and_logout(client: TestClient) -> None:
    headers = login(client)
    csrf = client.get("/api/v1/auth/session").json()["csrf"]
    assert csrf == headers["X-CSRF-Token"]
    assert client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN}).status_code == 403
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/status").status_code == 401


def test_password_change_revokes_every_session(client: TestClient) -> None:
    headers = login(client)
    first_cookie = client.cookies.get(COOKIE)
    headers = login(client)
    bad = {"old_password": "wrong", "new_password": "replacement-password-123"}
    assert client.post("/api/v1/auth/password", json=bad, headers=headers).status_code == 403
    bad["old_password"] = PASSWORD
    assert client.post("/api/v1/auth/password", json=bad, headers=headers).status_code == 200
    assert client.get("/api/v1/status").status_code == 401
    assert client.app.state.store.authenticate(first_cookie) is None
    assert client.app.state.store.login(PASSWORD) is None
    assert client.app.state.store.login(bad["new_password"]) is not None


def test_session_expiry_does_not_touch_on_poll(client: TestClient) -> None:
    login(client)
    store = client.app.state.store
    with Session(store.engine) as db, db.begin():
        row = db.scalars(select(BrowserSession)).one()
        touched = row.touched
    client.get("/api/v1/status")
    with Session(store.engine) as db, db.begin():
        row = db.scalars(select(BrowserSession)).one()
        assert row.touched == touched
        row.touched = time.time() - 1801
    assert client.get("/api/v1/status").status_code == 401


def test_wrong_password_rate_limit_and_size(client: TestClient) -> None:
    for _ in range(8):
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
                headers={"Origin": ORIGIN},
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": PASSWORD},
            headers={"Origin": ORIGIN},
        ).status_code
        == 429
    )
    assert (
        client.post(
            "/api/v1/auth/login", content=b"x" * 17000, headers={"Origin": ORIGIN}
        ).status_code
        == 413
    )


def test_storage_hashes_and_permissions(tmp_path: Path) -> None:
    store = Store(tmp_path / "state")
    assert not store.initialized()
    with pytest.raises(ValueError):
        store.initialize("short")
    store.initialize(PASSWORD)
    with pytest.raises(ValueError):
        store.initialize(PASSWORD)
    token = "test-only-device-token-with-32-chars"
    store.register_agent("laosan", token)
    assert store.agent_valid("laosan", token)
    assert not store.agent_valid("laosan", "bad")
    assert not store.agent_valid("other", token)
    with pytest.raises(ValueError):
        store.register_agent("laosan", token)
    with Session(store.engine) as db:
        admin = db.get(Admin, 1)
        assert admin.password_hash.startswith("$argon2id$")
        assert PASSWORD not in admin.password_hash
    assert (tmp_path / "state").stat().st_mode & 0o077 == 0
    assert (tmp_path / "state/console.sqlite3").stat().st_mode & 0o077 == 0
    store.audit("test.event")


def test_browser_and_device_websocket_auth(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/video", subprotocols=["console.v1"]),
    ):
        pass
    login(client)
    with client.websocket_connect(
        "wss://testserver/ws/video", headers={"Origin": ORIGIN}, subprotocols=["console.v1"]
    ) as socket:
        socket.send_json({"csrf": "bad"})
        assert socket.receive()["type"] == "websocket.close"
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/internal/v1/agents/laosan/control",
            headers={"Authorization": "Bearer bad"},
            subprotocols=["agent.v1"],
        ),
    ):
        pass


def test_initial_setup_has_no_default_password(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(tmp_path, origin=ORIGIN)), base_url=ORIGIN) as client:
        assert client.get("/health").json()["admin_initialized"] is False
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": PASSWORD},
                headers={"Origin": ORIGIN},
            ).status_code
            == 503
        )
