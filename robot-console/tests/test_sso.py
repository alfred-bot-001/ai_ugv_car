"""Home SSO boundaries, using mocked issuer transport and no hardware/network."""

import asyncio
import base64
import hashlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect
from tvpet_console.app import COOKIE, create_app
from tvpet_console.config import Settings
from tvpet_console.sso import TRANSACTION_COOKIE, SSOClient, SSOConfig, SSOError
from tvpet_console.store import Admin, Audit, BrowserSession, digest

ORIGIN = "https://car.aimaker.vip"
SECRET = "test-only-secret-which-is-at-least-32-characters"
PASSWORD = "test-only-original-password"
ISSUER_TOKEN = "issuer-test-opaque-access-token-at-least32"


def config_file(tmp_path: Path, **changes: str) -> Path:
    path = tmp_path / "sso.json"
    data = {
        "issuer_origin": "https://home.aimaker.vip",
        "backchannel_url": "http://127.0.0.1:80",
        "client_id": "car",
        "client_secret": SECRET,
        "redirect_uri": ORIGIN + "/auth/sso/callback",
    }
    data.update(changes)
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


class Issuer:
    def __init__(self) -> None:
        self.active = True
        self.role = "admin"
        self.fail = False
        self.mode = "ok"
        self.username = "家庭管理员"
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "127.0.0.1"
        assert request.headers["authorization"] == "Bearer " + SECRET
        assert request.headers["host"] == "home.aimaker.vip"
        assert request.headers["x-forwarded-proto"] == "https"
        assert "cookie" not in request.headers
        if self.fail:
            raise httpx.ConnectError("test unavailable")
        if self.mode == "redirect":
            return httpx.Response(307, headers={"location": "https://evil.invalid"})
        if self.mode == "large":
            return httpx.Response(200, content=b"x" * 17000)
        if self.mode == "bad-json":
            return httpx.Response(200, content=b"not json")
        if request.url.path == "/auth/sso/token":
            return httpx.Response(
                200,
                json={
                    "access_token": ISSUER_TOKEN,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "username": self.username,
                    "role": self.role,
                },
            )
        if request.url.path == "/auth/sso/revoke":
            self.active = False
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            200,
            json={
                "active": self.active,
                "username": self.username,
                "role": self.role,
                "expires_at": time.time() + 3600,
            },
        )


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(
        Settings(tmp_path / "state", origin=ORIGIN, sso_config_file=str(config_file(tmp_path)))
    )
    app.state.store.initialize(PASSWORD)
    issuer = Issuer()
    # Swap only the transport; retain production headers, timeouts, proxy and redirect policy.
    app.state.sso.http._transport = httpx.MockTransport(issuer.handle)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as browser:
        browser.issuer = issuer
        yield browser


def begin(client: TestClient, next_path: str = "/") -> tuple[str, dict[str, list[str]]]:
    response = client.get("/auth/sso/login", params={"next": next_path})
    assert response.status_code == 303
    url = response.headers["location"]
    assert url.startswith("https://home.aimaker.vip/auth/sso/authorize?")
    cookie = response.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("httponly", "secure", "samesite=strict", "max-age=300"))
    assert "domain=" not in cookie
    return client.cookies.get(TRANSACTION_COOKIE), parse_qs(urlparse(url).query)


def finish(client: TestClient, state: str) -> httpx.Response:
    return client.get("/auth/sso/callback", params={"state": state, "code": "test-code"})


def login(client: TestClient, next_path: str = "/") -> dict[str, str]:
    _, query = begin(client, next_path)
    response = finish(client, query["state"][0])
    assert response.status_code == 303
    assert response.headers["location"] == next_path
    session = client.get("/api/v1/auth/session")
    assert session.status_code == 200
    assert session.json()["username"] == "家庭管理员"
    assert session.json()["auth_mode"] == "sso"
    return {"Origin": ORIGIN, "X-CSRF-Token": session.json()["csrf"]}


def test_config_is_private_exact_and_fail_closed(tmp_path: Path) -> None:
    path = config_file(tmp_path)
    config = SSOConfig.load(str(path))
    assert SECRET not in repr(config)
    assert SSOConfig.load("") is None
    path.chmod(0o644)
    with pytest.raises(ValueError):
        SSOConfig.load(str(path))
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        SSOConfig.load(str(link))
    for values in (
        {"backchannel_url": "https://evil.invalid"},
        {"backchannel_url": "http://127.0.0.1:80/path"},
        {"issuer_origin": "https://evil.invalid"},
        {"redirect_uri": ORIGIN + "/evil"},
        {"client_id": "other"},
        {"client_secret": "short"},
    ):
        config_file(tmp_path, **values)
        with pytest.raises(ValueError):
            SSOConfig.load(str(path))
    path.write_bytes(b"x" * 17000)
    with pytest.raises(ValueError):
        SSOConfig.load(str(path))
    with pytest.raises(ValueError):
        create_app(
            Settings(
                tmp_path / "no-fallback",
                origin=ORIGIN,
                sso_config_file=str(tmp_path / "missing.json"),
            )
        )
    assert not (tmp_path / "no-fallback").exists()
    path = config_file(tmp_path)
    with pytest.raises(ValueError):
        create_app(
            Settings(
                tmp_path / "bad-origin",
                origin="http://localhost",
                secure_cookie=False,
                sso_config_file=str(path),
            )
        )


def test_config_rejects_wrong_owner(tmp_path: Path, monkeypatch) -> None:
    path = config_file(tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(ValueError):
        SSOConfig.load(str(path))


def test_pkce_cookie_return_path_and_no_credential_leaks(client: TestClient) -> None:
    assert client.get("/").headers["location"] == "/auth/sso/login?next=/"
    assert client.get("/photos").headers["location"] == "/auth/sso/login?next=/photos"
    assert client.get("/auth/sso/login?next=//evil.invalid").status_code == 400
    cookie, query = begin(client, "/photos")
    assert query["client_id"] == ["car"]
    assert query["code_challenge_method"] == ["S256"]
    transaction = client.app.state.sso.transactions[digest(cookie)]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(transaction.verifier.encode()).digest())
    assert query["code_challenge"] == [challenge.decode().rstrip("=")]
    response = finish(client, query["state"][0])
    assert response.status_code == 303
    assert response.headers["location"] == "/photos"
    exchange = json.loads(client.issuer.requests[-1].content)
    assert exchange["code_verifier"] == transaction.verifier
    assert exchange["redirect_uri"] == ORIGIN + "/auth/sso/callback"
    assert exchange["grant_type"] == "authorization_code"
    assert TRANSACTION_COOKIE not in client.cookies
    assert COOKIE in client.cookies
    for value in (SECRET, ISSUER_TOKEN, transaction.verifier):
        assert value not in response.text + str(response.headers)
        assert value not in repr(client.app.state.sso.grants)
    session = client.get("/api/v1/auth/session").json()
    assert session["username"] == "家庭管理员"
    page = client.get("/").text
    assert "家庭管理员" in page and "家庭账号 · 修改密码" in page
    assert 'id="password-open" hidden' in page
    with Session(client.app.state.store.engine) as db:
        admin = db.get(Admin, 1)
        assert admin.generation == 1
        assert all(ISSUER_TOKEN not in row.token_hash for row in db.scalars(select(BrowserSession)))
        assert all(SECRET not in row.event for row in db.scalars(select(Audit)))
    assert client.app.state.store.login(PASSWORD) is not None


@pytest.mark.parametrize("failure", ["state", "cookie", "expired", "nonascii"])
def test_state_bound_single_use_transactions(client: TestClient, failure: str) -> None:
    cookie, query = begin(client)
    state = query["state"][0]
    if failure == "state":
        state = "wrong"
    elif failure == "cookie":
        client.cookies.delete(TRANSACTION_COOKIE)
    elif failure == "nonascii":
        state = "汉字"
    else:
        transactions = client.app.state.sso.transactions
        transactions[digest(cookie)] = replace(transactions[digest(cookie)], created=0)
    assert finish(client, state).status_code == 400
    assert not client.issuer.requests
    assert COOKIE not in client.cookies
    assert finish(client, query["state"][0]).status_code == 400


def test_callback_cannot_be_replayed(client: TestClient) -> None:
    cookie, query = begin(client)
    assert finish(client, query["state"][0]).status_code == 303
    client.cookies.set(TRANSACTION_COOKIE, cookie, domain="car.aimaker.vip", path="/auth/sso")
    assert finish(client, query["state"][0]).status_code == 400
    assert len(client.issuer.requests) == 1


def test_viewer_role_is_not_promoted(client: TestClient) -> None:
    client.issuer.role = "viewer"
    _, query = begin(client)
    assert finish(client, query["state"][0]).status_code == 403
    assert COOKIE not in client.cookies
    assert client.get("/api/v1/status").status_code == 401


def test_local_auth_bypasses_disabled_and_csrf_retained(client: TestClient) -> None:
    old_token, _ = client.app.state.store.login(PASSWORD)
    client.cookies.set(COOKIE, old_token, domain="car.aimaker.vip", path="/")
    assert client.get("/api/v1/status").status_code == 401
    body = {"username": "admin", "password": PASSWORD}
    assert (
        client.post("/api/v1/auth/login", json=body, headers={"Origin": ORIGIN}).status_code == 403
    )
    headers = login(client)
    assert (
        client.post(
            "/api/v1/auth/password",
            json={"old_password": PASSWORD, "new_password": "replacement-12chars"},
            headers=headers,
        ).status_code
        == 403
    )
    assert client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN}).status_code == 403
    assert (
        client.post(
            "/api/v1/auth/logout", headers={**headers, "Origin": "https://other.aimaker.vip"}
        ).status_code
        == 403
    )
    assert client.post("/api/v1/control/stop", headers=headers).status_code == 409
    assert client.get("/api/v1/auth/session").status_code == 200
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert not client.issuer.active
    assert client.get("/api/v1/status").status_code == 401
    assert COOKIE not in client.cookies


@pytest.mark.parametrize(
    "failure", ["revoked", "unavailable", "demoted", "changed-user", "expired", "restart"]
)
def test_every_http_auth_is_live_and_fails_closed(client: TestClient, failure: str) -> None:
    login(client)
    # A fresh HTTP request must not use the successful check cached by the previous request.
    if failure == "revoked":
        client.issuer.active = False
    elif failure == "unavailable":
        client.issuer.fail = True
    elif failure == "demoted":
        client.issuer.role = "viewer"
    elif failure == "changed-user":
        client.issuer.username = "someone-else"
    elif failure == "expired":
        for grant in client.app.state.sso.grants.values():
            grant.expires_at = 0
    else:
        client.app.state.sso.grants.clear()
    assert client.get("/api/v1/auth/session").status_code == 401
    assert client.get("/api/v1/photos").status_code == 401
    assert client.get("/api/v1/photo-labels/export").status_code == 401
    assert client.get("/api/v1/status").status_code == 401


@pytest.mark.parametrize("mode", ["redirect", "large", "bad-json"])
def test_bad_issuer_responses_fail_without_following_redirects(
    client: TestClient, mode: str
) -> None:
    client.issuer.mode = mode
    _, query = begin(client)
    response = finish(client, query["state"][0])
    assert response.status_code == 400
    assert len(client.issuer.requests) == 1
    assert SECRET not in response.text and ISSUER_TOKEN not in response.text


def test_browser_control_revocation_closes_socket_without_commands(client: TestClient) -> None:
    headers = login(client)
    with client.websocket_connect(
        "wss://car.aimaker.vip/ws/control", headers={"Origin": ORIGIN}, subprotocols=["console.v1"]
    ) as socket:
        socket.send_json({"csrf": headers["X-CSRF-Token"]})
        assert "challenge" in socket.receive_json()
        start = time.monotonic()
        client.issuer.active = False
        while True:
            message = socket.receive()
            if message["type"] == "websocket.close":
                break
            assert time.monotonic() - start < 5
        assert time.monotonic() - start < 5
    assert client.get("/api/v1/status").status_code == 401


def test_websocket_origin_csrf_and_old_local_session_rejected(client: TestClient) -> None:
    old_token, _ = client.app.state.store.login(PASSWORD)
    client.cookies.set(COOKIE, old_token, domain="car.aimaker.vip", path="/")
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("wss://car.aimaker.vip/ws/control", headers={"Origin": ORIGIN}),
    ):
        pass
    login(client)
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "wss://car.aimaker.vip/ws/control", headers={"Origin": "https://evil.invalid"}
        ),
    ):
        pass
    with client.websocket_connect(
        "wss://car.aimaker.vip/ws/control", headers={"Origin": ORIGIN}, subprotocols=["console.v1"]
    ) as socket:
        socket.send_json({"csrf": "wrong"})
        assert socket.receive()["type"] == "websocket.close"


def test_logout_failure_still_clears_local_access(client: TestClient) -> None:
    headers = login(client)
    original = client.issuer.handle

    def fail_revoke(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/sso/revoke":
            raise httpx.ConnectError("unavailable")
        return original(request)

    client.app.state.sso.http._transport = httpx.MockTransport(fail_revoke)
    response = client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 503
    assert COOKIE not in client.cookies
    assert client.get("/api/v1/status").status_code == 401


def test_sso_disabled_keeps_existing_local_routes(tmp_path: Path) -> None:
    app = create_app(Settings(tmp_path, origin=ORIGIN))
    app.state.store.initialize(PASSWORD)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/auth/sso/login").status_code == 404
        assert client.get("/auth/sso/callback").status_code == 404
        assert (
            client.post(
                "/api/v1/auth/login",
                headers={"Origin": ORIGIN},
                json={"username": "admin", "password": PASSWORD},
            ).status_code
            == 200
        )
        assert client.get("/api/v1/auth/session").json()["auth_mode"] == "local"


def test_transaction_limit_and_expiry(tmp_path: Path) -> None:
    config = SSOConfig.load(str(config_file(tmp_path)))
    assert config is not None
    client = SSOClient(config)
    try:
        for _ in range(1024):
            client.begin("/")
        with pytest.raises(SSOError):
            client.begin("/")
        client.transactions = {
            key: replace(value, created=0) for key, value in client.transactions.items()
        }
        client.begin("/")
        assert len(client.transactions) == 1
    finally:
        asyncio.run(client.close())


def test_logout_does_not_require_available_introspection(client: TestClient) -> None:
    headers = login(client)
    client.issuer.requests.clear()
    client.issuer.fail = True
    response = client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 503
    assert COOKIE not in client.cookies
    assert [request.url.path for request in client.issuer.requests] == ["/auth/sso/revoke"]
    assert client.get("/api/v1/status").status_code == 401


@pytest.mark.parametrize("unavailable", [False, True])
def test_active_media_closes_on_home_revocation_or_outage(
    client: TestClient, unavailable: bool, monkeypatch
) -> None:
    from unittest.mock import AsyncMock

    headers = login(client)
    gateway = client.app.state.gateway
    # Simulated local device acknowledgements; no real hardware transport is created.
    gateway.control = AsyncMock()
    gateway.state_at = time.monotonic()
    rpc = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(gateway, "rpc", rpc)
    with client.websocket_connect(
        "wss://car.aimaker.vip/ws/video", headers={"Origin": ORIGIN}, subprotocols=["console.v1"]
    ) as socket:
        socket.send_json({"csrf": headers["X-CSRF-Token"]})
        deadline = time.monotonic() + 2
        while gateway.viewer_counts["video"] != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gateway.viewer_counts["video"] == 1
        start = time.monotonic()
        if unavailable:
            client.issuer.fail = True
        else:
            client.issuer.active = False
        assert socket.receive()["type"] == "websocket.close"
        assert time.monotonic() - start < 5
    calls = [(call.args, call.kwargs) for call in rpc.await_args_list]
    assert any(args and args[0] == "stop" for args, _ in calls)
    assert any(args == ("media.set", {"channel": "video", "enabled": False}) for args, _ in calls)


def test_logout_captures_grant_before_inflight_auth_cleanup(
    client: TestClient, monkeypatch
) -> None:
    headers = login(client)
    sso = client.app.state.sso

    async def competing_cleanup() -> None:
        # Models a request that started before logout and completes during device revocation.
        sso.grants.clear()

    monkeypatch.setattr(client.app.state.gateway, "revoke", competing_cleanup)
    response = client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 200
    assert client.issuer.active is False
    assert client.issuer.requests[-1].url.path == "/auth/sso/revoke"


def test_logout_does_not_claim_upstream_success_for_missing_grant(client: TestClient) -> None:
    headers = login(client)
    client.app.state.sso.grants.clear()
    response = client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 503
    assert COOKIE not in client.cookies
    assert client.issuer.active is True
