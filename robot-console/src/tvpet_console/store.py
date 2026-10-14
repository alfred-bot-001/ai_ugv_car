"""Hashed credentials, expiring sessions and metadata-only audit records."""

import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import Engine, create_engine, delete, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_proof(token: str) -> str:
    return digest("csrf:" + token)


class Base(DeclarativeBase):
    pass


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(primary_key=True)
    password_hash: Mapped[str]
    generation: Mapped[int] = mapped_column(default=1)


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    token_hash: Mapped[str] = mapped_column(primary_key=True)
    csrf_hash: Mapped[str]
    generation: Mapped[int]
    created: Mapped[float]
    touched: Mapped[float]


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(primary_key=True)
    token_hash: Mapped[str]


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[float]
    event: Mapped[str]


@dataclass(frozen=True)
class Identity:
    token_hash: str
    generation: int
    username: str = "admin"
    auth_mode: str = "local"


class Store:
    """SQLite store. No media, speech text or plaintext passwords are persisted."""

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(data_dir, 0o700)
        self.engine: Engine = create_engine(
            f"sqlite:///{data_dir / 'console.sqlite3'}",
            connect_args={"check_same_thread": False, "timeout": 5},
        )
        from .migrations import upgrade

        upgrade(self.engine)
        os.chmod(data_dir / "console.sqlite3", 0o600)

    def initialized(self) -> bool:
        with Session(self.engine) as db:
            return db.get(Admin, 1) is not None

    def initialize(self, password: str) -> None:
        if not 12 <= len(password) <= 256:
            raise ValueError("密码长度必须为 12–256 个字符")
        password_hash = HASHER.hash(password)
        with Session(self.engine) as db, db.begin():
            if db.get(Admin, 1):
                raise ValueError("admin 已存在，请通过页面验证旧密码后修改")
            db.add(Admin(id=1, password_hash=password_hash, generation=1))

    def login(self, password: str) -> tuple[str, str] | None:
        with Session(self.engine) as db, db.begin():
            admin = db.get(Admin, 1)
            try:
                HASHER.verify(admin.password_hash if admin else DUMMY_HASH, password)
            except VerificationError:
                return None
            if not admin:
                return None
            token = secrets.token_urlsafe(32)
            csrf = csrf_proof(token)
            now = time.time()
            db.execute(delete(BrowserSession).where(BrowserSession.created < now - 43200))
            db.add(
                BrowserSession(
                    token_hash=digest(token),
                    csrf_hash=digest(csrf),
                    generation=admin.generation,
                    created=now,
                    touched=now,
                )
            )
            db.add(Audit(at=now, event="auth.login"))
            return token, csrf

    def login_sso(self) -> tuple[str, str]:
        """Issue the ordinary local session without altering admin credentials/generation."""
        with Session(self.engine) as db, db.begin():
            admin = db.get(Admin, 1)
            if admin is None:
                raise ValueError("请先初始化主控台")
            token = secrets.token_urlsafe(32)
            csrf = csrf_proof(token)
            now = time.time()
            db.execute(delete(BrowserSession).where(BrowserSession.created < now - 43200))
            db.add(
                BrowserSession(
                    token_hash=digest(token),
                    csrf_hash=digest(csrf),
                    generation=admin.generation,
                    created=now,
                    touched=now,
                )
            )
            db.add(Audit(at=now, event="auth.sso_login"))
            return token, csrf

    def authenticate(self, token: str, *, touch: bool = False) -> Identity | None:
        now = time.time()
        with Session(self.engine) as db, db.begin():
            row = db.get(BrowserSession, digest(token))
            admin = db.get(Admin, 1)
            if (
                not row
                or not admin
                or row.generation != admin.generation
                or now - row.created >= 43200
                or now - row.touched >= 1800
            ):
                return None
            if touch:
                row.touched = now
            return Identity(row.token_hash, row.generation)

    def csrf_valid(self, identity: Identity, csrf: str) -> bool:
        with Session(self.engine) as db:
            row = db.get(BrowserSession, identity.token_hash)
            return row is not None and secrets.compare_digest(row.csrf_hash, digest(csrf))

    def logout(self, identity: Identity) -> None:
        with Session(self.engine) as db, db.begin():
            db.execute(
                delete(BrowserSession).where(BrowserSession.token_hash == identity.token_hash)
            )
            db.add(Audit(at=time.time(), event="auth.logout"))

    def change_password(self, old: str, new: str) -> bool:
        if not 12 <= len(new) <= 256:
            raise ValueError("密码长度必须为 12–256 个字符")
        with Session(self.engine) as db, db.begin():
            admin = db.get(Admin, 1)
            if not admin:
                return False
            try:
                HASHER.verify(admin.password_hash, old)
            except VerificationError:
                return False
            admin.password_hash = HASHER.hash(new)
            admin.generation += 1
            db.execute(delete(BrowserSession))
            db.add(Audit(at=time.time(), event="auth.password_changed"))
            return True

    def register_agent(self, agent_id: str, token: str) -> None:
        with Session(self.engine) as db, db.begin():
            if db.get(Agent, agent_id):
                raise ValueError("设备已注册；轮换 token 需先停用现有连接")
            db.add(Agent(id=agent_id, token_hash=digest(token)))

    def agent_valid(self, agent_id: str, token: str) -> bool:
        with Session(self.engine) as db:
            row = db.get(Agent, agent_id)
            return row is not None and secrets.compare_digest(row.token_hash, digest(token))

    def audit(self, event: str) -> None:
        with Session(self.engine) as db, db.begin():
            db.add(Audit(at=time.time(), event=event))
            # Bounded metadata history; never includes command arguments or user text.
            keep = select(Audit.id).order_by(Audit.id.desc()).limit(10000)
            db.execute(delete(Audit).where(Audit.id.not_in(keep)))
