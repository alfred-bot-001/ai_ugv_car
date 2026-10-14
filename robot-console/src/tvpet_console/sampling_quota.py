"""Persistent daily photo limits with exclusive reservations and crash recovery."""

import fcntl
import json
import math
import os
import re
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SINGAPORE = ZoneInfo("Asia/Singapore")
SAMPLE_ID = re.compile(r"\d{8}-\d{6}-\d{6}\Z")
JSON_LIMIT = 1024 * 1024


class DailyLimitReached(ValueError):
    """The current Singapore day has already consumed all successful saves."""


def _safe(path: Path) -> None:
    for component in [*reversed(path.parents), path]:
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError("采样配额与照片路径不可使用符号链接")


def _json(path: Path) -> dict[str, Any]:
    _safe(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > JSON_LIMIT:
            raise ValueError("采样记录格式或大小无效")
        data = stream.read(JSON_LIMIT + 1)
    if len(data) > JSON_LIMIT:
        raise ValueError("采样记录超过大小限制")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("采样记录损坏，已暂停采样") from error
    if not isinstance(value, dict):
        raise ValueError("采样记录无效，已暂停采样")
    return value


def _sample_day(record: dict[str, Any]) -> str:
    value = record.get("sampled_at")
    if not isinstance(value, str):
        raise ValueError("采样时间缺失，已暂停采样")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("采样时间缺少时区，已暂停采样")
    return moment.astimezone(SINGAPORE).date().isoformat()


class SamplingQuota:
    """Every disk operation is synchronous and must run in a worker thread."""

    def __init__(self, samples: Path, state_dir: Path, limit: int = 50) -> None:
        if not 1 <= limit <= 50:
            raise ValueError("每日采样上限必须为 1 到 50")
        self.samples = samples.absolute()
        self.state_dir = state_dir.absolute()
        self.root = self.state_dir / "sampling-quota"
        self.path = self.root / "quota.sqlite3"
        self.limit = limit
        self.lock = threading.RLock()
        self.days: dict[str, int] = {}
        self.error = ""
        self.initialized = False
        self.failed: set[str] = set()
        self.last_saved_epoch = 0.0

    @staticmethod
    def day(moment: datetime) -> str:
        if moment.tzinfo is None:
            raise ValueError("采样配额时间必须带时区")
        return moment.astimezone(SINGAPORE).date().isoformat()

    def snapshot(self, moment: datetime) -> dict[str, Any]:
        day = self.day(moment)
        return {
            "daily_saved": self.days.get(day, 0) if self.initialized else None,
            "daily_limit": self.limit,
            "day": day,
            "timezone": SINGAPORE.key,
        }

    @contextmanager
    def _locked(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            descriptor = None
            connection = None
            try:
                _safe(self.root)
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.root.chmod(0o700)
                lock_path = self.root / ".lock"
                _safe(lock_path)
                descriptor = os.open(
                    lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
                )
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("采样配额锁文件无效")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                _safe(self.path)
                database_fd = os.open(
                    self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
                )
                try:
                    info = os.fstat(database_fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
                        raise ValueError("采样配额账本无效")
                    os.fchmod(database_fd, 0o600)
                finally:
                    os.close(database_fd)
                connection = sqlite3.connect(self.path, timeout=10)
                connection.execute("PRAGMA synchronous=FULL")
                self._initialize(connection)
                yield connection
            except sqlite3.Error as error:
                self.error = "采样配额账本不可读写，已暂停采样"
                raise OSError(self.error) from error
            except DailyLimitReached:
                raise
            except (OSError, ValueError) as error:
                self.error = str(error)
                raise
            finally:
                if connection is not None:
                    connection.close()
                if descriptor is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)

    def _initialize(self, connection: sqlite3.Connection) -> None:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not tables:
            connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("CREATE TABLE days (day TEXT PRIMARY KEY, saved INTEGER)")
            connection.execute(
                "CREATE TABLE pending (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "sample_id TEXT, day TEXT, moment TEXT)"
            )
            connection.execute("CREATE TABLE unresolved (sample_id TEXT PRIMARY KEY, day TEXT)")
        expected = {
            "meta": ["key", "value"],
            "days": ["day", "saved"],
            "pending": ["singleton", "sample_id", "day", "moment"],
            "unresolved": ["sample_id", "day"],
        }
        for table, columns in expected.items():
            if [row[1] for row in connection.execute(f"PRAGMA table_info({table})")] != columns:
                raise ValueError("采样配额账本结构损坏，已暂停采样")
        schema = connection.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if schema is None:
            counts = self._migrate()
            with connection:
                connection.executemany("INSERT INTO days(day,saved) VALUES (?,?)", counts.items())
                connection.execute("INSERT INTO meta(key,value) VALUES ('schema','1')")
                connection.execute(
                    "INSERT INTO meta(key,value) VALUES ('timezone','Asia/Singapore')"
                )
                connection.execute(
                    "INSERT INTO meta(key,value) VALUES ('samples',?)", (str(self.samples),)
                )
        elif schema[0] != "1":
            raise ValueError("采样配额账本版本不受支持")
        identity = dict(connection.execute("SELECT key,value FROM meta"))
        if identity.get("timezone") != SINGAPORE.key or identity.get("samples") != str(
            self.samples
        ):
            raise ValueError("采样配额目录或时区与账本不一致，已暂停采样")
        self._cache(connection)

    def _migrate(self) -> dict[str, int]:
        """Count unique committed source IDs, including already recycled snapshots."""
        candidates: list[tuple[str, Path, Path]] = []
        _safe(self.samples)
        if self.samples.is_dir():
            candidates.extend(
                (path.stem, path, path.with_suffix(".jpg"))
                for path in self.samples.glob("*.json")
                if SAMPLE_ID.fullmatch(path.stem)
            )
        labels = self.state_dir / "photo-labels"
        for directory, patterns in [
            (labels / "samples", ["*/model.json"]),
            (labels / "trash", ["*/*/source-model.json", "*/*/bundle/model.json"]),
        ]:
            _safe(directory)
            if not directory.is_dir():
                continue
            for pattern in patterns:
                for path in directory.glob(pattern):
                    sample_id = (
                        path.parent.parent.name
                        if path.parent.name == "bundle"
                        else path.parent.name
                    )
                    if not SAMPLE_ID.fullmatch(sample_id):
                        continue
                    picture = path.with_name(
                        "source-annotated.jpg"
                        if path.name == "source-model.json"
                        else "annotated.jpg"
                    )
                    candidates.append((sample_id, path, picture))
        seen: dict[str, str] = {}
        total = 0
        for sample_id, metadata, picture in candidates:
            _safe(metadata)
            _safe(picture)
            total += metadata.stat().st_size
            if len(candidates) > 20000 or total > 64 * 1024 * 1024:
                raise ValueError("历史采样迁移超过读取上限，已暂停采样")
            if not picture.is_file():
                raise ValueError("历史采样缺少完整照片，已暂停采样")
            day = _sample_day(_json(metadata))
            if sample_id in seen and seen[sample_id] != day:
                raise ValueError("历史样本备份日期不一致，已暂停采样")
            seen[sample_id] = day
        counts: dict[str, int] = {}
        for day in seen.values():
            counts[day] = counts.get(day, 0) + 1
        return counts

    def _cache(self, connection: sqlite3.Connection) -> None:
        counts = {}
        for day, saved in connection.execute("SELECT day,saved FROM days"):
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise ValueError("采样配额日期损坏")
            datetime.strptime(day, "%Y-%m-%d")
            if not isinstance(saved, int) or saved < 0:
                raise ValueError("采样配额计数损坏")
            counts[day] = saved
        self.days = counts
        value = connection.execute("SELECT value FROM meta WHERE key='last_saved_epoch'").fetchone()
        if value is not None and not isinstance(value[0], str):
            raise ValueError("采样配额时间损坏")
        self.last_saved_epoch = float(value[0]) if value else 0.0
        if not math.isfinite(self.last_saved_epoch) or self.last_saved_epoch < 0:
            raise ValueError("采样配额时间损坏")
        self.initialized = True

    def _proof(self, sample_id: str, day: str) -> bool:
        metadata = self.samples / f"{sample_id}.json"
        _safe(metadata)
        if metadata.is_file():
            record = _json(metadata)
            if _sample_day(record) != day:
                raise ValueError("待恢复采样日期不一致")
            picture = metadata.with_suffix(".jpg")
            _safe(picture)
            if not picture.is_file():
                raise OSError("待恢复采样照片不完整，已暂停采样")
            if record.get("original_available") is True:
                original = self.samples / "originals" / f"{sample_id}.jpg"
                _safe(original)
                if not original.is_file():
                    raise OSError("待恢复采样原片不完整，已暂停采样")
            return True
        # Product deletion writes a durable marker only after observing a complete sample.
        deleted = self.state_dir / "photo-labels" / "deleted" / f"{sample_id}.json"
        _safe(deleted)
        if deleted.is_file():
            record = _json(deleted)
            if record.get("id") == sample_id and record.get("scope") == "photos":
                return True
        return False

    def _pending(self, connection: sqlite3.Connection) -> tuple[str, str, datetime] | None:
        row = connection.execute(
            "SELECT sample_id,day,moment FROM pending WHERE singleton=1"
        ).fetchone()
        if row is None:
            return None
        sample_id, day, sampled_at = row
        if not isinstance(sample_id, str) or not SAMPLE_ID.fullmatch(sample_id):
            raise ValueError("采样预留记录损坏")
        if not isinstance(day, str):
            raise ValueError("采样预留日期损坏")
        datetime.strptime(day, "%Y-%m-%d")
        if _sample_day({"sampled_at": sampled_at}) != day:
            raise ValueError("采样预留时间损坏")
        return sample_id, day, datetime.fromisoformat(sampled_at)

    def _complete(
        self, connection: sqlite3.Connection, sample_id: str, day: str, moment: datetime
    ) -> None:
        with connection:
            connection.execute(
                "INSERT INTO days(day,saved) VALUES (?,1) "
                "ON CONFLICT(day) DO UPDATE SET saved=saved+1",
                (day,),
            )
            connection.execute(
                "DELETE FROM pending WHERE singleton=1 AND sample_id=?", (sample_id,)
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES ('last_saved_epoch',?)",
                (str(moment.timestamp()),),
            )
        self._cache(connection)

    def _recover(self, connection: sqlite3.Connection, moment: datetime) -> None:
        pending = self._pending(connection)
        if pending is None:
            return
        sample_id, day, saved_at = pending
        try:
            committed = self._proof(sample_id, day)
        except (OSError, ValueError):
            if day == self.day(moment):
                raise
            committed = False
        if committed:
            self._complete(connection, sample_id, day, saved_at)
        elif sample_id in self.failed:
            with connection:
                connection.execute("DELETE FROM pending WHERE singleton=1")
            self.failed.discard(sample_id)
        elif day != self.day(moment):
            # Keep uncertain historical reservations for audit; they cannot consume today's slots.
            with connection:
                connection.execute(
                    "INSERT OR IGNORE INTO unresolved(sample_id,day) VALUES (?,?)", (sample_id, day)
                )
                connection.execute("DELETE FROM pending WHERE singleton=1")
        else:
            raise OSError("上次采样提交状态无法确认，已暂停当日采样以防超过配额")
        self._cache(connection)

    def refresh(self, moment: datetime) -> None:
        with self._locked() as connection:
            self._recover(connection, moment)
            self._cache(connection)
            self.error = ""

    @contextmanager
    def slot(self, moment: datetime, sample_id: str) -> Iterator[None]:
        if not SAMPLE_ID.fullmatch(sample_id):
            raise ValueError("采样编号无效")
        with self._locked() as connection:
            self._recover(connection, moment)
            day = self.day(moment)
            if self.days.get(day, 0) >= self.limit:
                self.error = ""
                raise DailyLimitReached("今日已达到采样上限，明日自动恢复")
            for destination in [
                self.samples / f"{sample_id}.json",
                self.samples / f"{sample_id}.jpg",
                self.samples / "originals" / f"{sample_id}.jpg",
                self.state_dir / "photo-labels" / "deleted" / f"{sample_id}.json",
            ]:
                _safe(destination)
                if destination.exists():
                    raise FileExistsError("审核照片已存在，不覆盖已有样本")
            with connection:
                connection.execute(
                    "INSERT INTO pending(singleton,sample_id,day,moment) VALUES (1,?,?,?)",
                    (sample_id, day, moment.isoformat()),
                )
            try:
                yield
            except BaseException:
                self.failed.add(sample_id)
                # Never release a slot if its JSON was published despite an interrupted caller.
                if self._proof(sample_id, day):
                    self._complete(connection, sample_id, day, moment)
                else:
                    with connection:
                        connection.execute("DELETE FROM pending WHERE singleton=1")
                    self.failed.discard(sample_id)
                    self._cache(connection)
                raise
            else:
                self._complete(connection, sample_id, day, moment)
                self.error = ""
