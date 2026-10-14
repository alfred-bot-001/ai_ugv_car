"""Private, loopback-only SSO client; grants and login transactions stay in memory."""

import asyncio
import base64
import hashlib
import json
import math
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from .store import digest

TRANSACTION_COOKIE = "tvpet_sso_transaction"
TRANSACTION_TTL = 300
CHECK_TTL = 2.0
MAX_ENTRIES = 1024


@dataclass(frozen=True)
class SSOConfig:
    issuer_origin: str
    backchannel_url: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str = "https://car.aimaker.vip/auth/sso/callback"

    @classmethod
    def load(cls, filename: str) -> "SSOConfig | None":
        if not filename:
            return None
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 16384
                ):
                    raise ValueError
                raw = source.read(16385)
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {
                "issuer_origin",
                "backchannel_url",
                "client_id",
                "client_secret",
                "redirect_uri",
            }:
                raise ValueError
            if (
                data["issuer_origin"] != "https://home.aimaker.vip"
                or data["backchannel_url"] != "http://127.0.0.1:80"
                or data["client_id"] != "car"
                or data["redirect_uri"] != "https://car.aimaker.vip/auth/sso/callback"
                or not isinstance(data["client_secret"], str)
                or not 32 <= len(data["client_secret"]) <= 256
                or not data["client_secret"].isascii()
                or any(ord(char) <= 32 or ord(char) >= 127 for char in data["client_secret"])
            ):
                raise ValueError
            return cls(**data)
        except (OSError, ValueError, TypeError) as error:
            raise ValueError("SSO 配置必须是当前用户所有的 0600 私有文件且内容有效") from error


class SSOError(Exception):
    """Never includes issuer responses, credentials, codes or verifier values."""


@dataclass(frozen=True)
class Transaction:
    state: str
    verifier: str = field(repr=False)
    next_path: str = "/"
    created: float = field(default_factory=time.monotonic)


@dataclass
class Grant:
    token: str = field(repr=False)
    username: str = ""
    expires_at: float = 0
    checked: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def valid_username(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def positive_number(value: Any) -> bool:
    return type(value) in (int, float) and 0 < value <= 10**12 and math.isfinite(value)


class SSOClient:
    def __init__(self, config: SSOConfig) -> None:
        self.config = config
        self.transactions: dict[str, Transaction] = {}
        self.grants: dict[str, Grant] = {}
        self.http = httpx.AsyncClient(
            base_url=config.backchannel_url,
            headers={
                "Authorization": "Bearer " + config.client_secret,
                "Host": "home.aimaker.vip",
                "X-Forwarded-Proto": "https",
            },
            timeout=httpx.Timeout(1.5),
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self) -> None:
        self.transactions.clear()
        self.grants.clear()
        await self.http.aclose()

    def begin(self, next_path: str) -> tuple[str, str]:
        if next_path not in ("/", "/photos"):
            raise SSOError("登录返回地址无效")
        now = time.monotonic()
        self.transactions = {
            key: value
            for key, value in self.transactions.items()
            if now - value.created < TRANSACTION_TTL
        }
        if len(self.transactions) >= MAX_ENTRIES:
            raise SSOError("登录请求过多，请稍后重试")
        cookie, state, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        self.transactions[digest(cookie)] = Transaction(state, verifier, next_path)
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "state": state,
                "code_challenge": challenge.decode().rstrip("="),
                "code_challenge_method": "S256",
            }
        )
        return cookie, self.config.issuer_origin + "/auth/sso/authorize?" + query

    async def post(self, path: str, body: dict[str, str]) -> dict[str, Any]:
        try:
            # wait_for also bounds a slow, continuously streaming response.
            async with asyncio.timeout(2):
                async with self.http.stream("POST", path, json=body) as response:
                    if response.status_code != 200:
                        raise SSOError("家庭控制台认证暂不可用，请重新登录")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 16384:
                            raise SSOError("家庭控制台认证响应无效")
                    data = json.loads(payload)
            if not isinstance(data, dict):
                raise SSOError("家庭控制台认证响应无效")
            return data
        except (httpx.HTTPError, TimeoutError, ValueError) as error:
            raise SSOError("家庭控制台认证暂不可用，请重新登录") from error

    async def exchange(self, cookie: str, state: str, code: str) -> tuple[Grant, str]:
        transaction = self.transactions.pop(digest(cookie), None)
        if (
            transaction is None
            or time.monotonic() - transaction.created >= TRANSACTION_TTL
            or not state
            or not state.isascii()
            or not secrets.compare_digest(state, transaction.state)
            or not code
            or len(code) > 1024
        ):
            raise SSOError("登录请求已过期或校验失败，请重新登录")
        data = await self.post(
            "/auth/sso/token",
            {
                "grant_type": "authorization_code",
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "code": code,
                "code_verifier": transaction.verifier,
            },
        )
        if data.get("role") != "admin":
            raise SSOError("设备控制台仅允许家庭管理员访问")
        if (
            data.get("token_type") != "Bearer"
            or not isinstance(data.get("access_token"), str)
            or not 32 <= len(data["access_token"]) <= 1024
            or not valid_username(data.get("username"))
            or not positive_number(data.get("expires_in"))
            or data["expires_in"] > 43200
        ):
            raise SSOError("家庭控制台认证响应无效")
        return Grant(
            data["access_token"], data["username"], time.time() + data["expires_in"]
        ), transaction.next_path

    def link(self, session_hash: str, grant: Grant) -> None:
        now = time.time()
        self.grants = {key: value for key, value in self.grants.items() if value.expires_at > now}
        if len(self.grants) >= MAX_ENTRIES:
            raise SSOError("登录会话过多，请稍后重试")
        self.grants[session_hash] = grant

    async def valid(self, session_hash: str, *, cached: bool = False) -> Grant | None:
        grant = self.grants.get(session_hash)
        if grant is None:
            return None
        async with grant.lock:
            if self.grants.get(session_hash) is not grant or grant.expires_at <= time.time():
                self.grants.pop(session_hash, None)
                return None
            if cached and time.monotonic() - grant.checked < CHECK_TTL:
                return grant
            try:
                checked = time.monotonic()
                data = await self.post("/auth/sso/introspect", {"token": grant.token})
                if (
                    data.get("active") is not True
                    or data.get("role") != "admin"
                    or data.get("username") != grant.username
                    or not positive_number(data.get("expires_at"))
                    or data["expires_at"] <= time.time()
                    or grant.expires_at <= time.time()
                    or self.grants.get(session_hash) is not grant
                ):
                    raise SSOError("家庭控制台会话已失效")
                grant.expires_at = min(grant.expires_at, data["expires_at"])
                grant.checked = checked
                return grant
            except SSOError:
                self.grants.pop(session_hash, None)
                return None

    async def logout(self, grant: Grant | None) -> bool:
        if grant is None:
            return False
        try:
            await self.post("/auth/sso/revoke", {"token": grant.token})
            return True
        except SSOError:
            return False
