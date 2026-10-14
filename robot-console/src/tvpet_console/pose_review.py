"""Private accuracy samples with exact source images and same-frame recognition data."""

import asyncio
import contextlib
import io
import json
import os
import shutil
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from tvpet_pi.pose import CONNECTIONS
from tvpet_pi.protocol import MAX_JPEG

from .sampling_quota import SINGAPORE, DailyLimitReached, SamplingQuota


def associate(
    pose: dict[str, Any], faces: list[dict[str, Any]], width: int = 640, height: int = 480
) -> dict[str, Any]:
    """Only unique face/head matches. No identity guessing or cross-frame carryover."""
    people = []
    matches: list[list[int]] = []
    for person in pose.get("people", []):
        points = person["keypoints"]
        nose = points[0]
        candidates = []
        if nose[3] >= 0.6:
            x, y = nose[0] * width, nose[1] * height
            for i, face in enumerate(faces):
                x1, y1, x2, y2 = face["bbox"]
                if x1 <= x <= x2 and y1 <= y <= y2:
                    candidates.append(i)
        matches.append(candidates)
    for person, candidates in zip(pose.get("people", []), matches, strict=True):
        matched_face: dict[str, Any] | None = None
        if len(candidates) == 1 and sum(candidates[0] in others for others in matches) == 1:
            matched_face = faces[candidates[0]]
        people.append(
            {
                **person,
                "identity": matched_face["label"] if matched_face else "人物未确认",
                "identity_known": bool(matched_face and matched_face.get("known")),
            }
        )
    return {**pose, "people": people}


class ReviewRecorder:
    def __init__(
        self,
        directory: str = "",
        font: str = "",
        interval: float = 30,
        *,
        state_dir: Path | None = None,
        daily_limit: int = 50,
    ) -> None:
        self.directory = Path(directory) if directory else None
        self.font = font
        self.interval = interval
        self.state = "waiting" if directory else "disabled"
        self.error = ""
        self.saved = 0
        self.last_saved = ""
        self.next_at = 0.0
        if self.directory and not self.directory.is_symlink() and self.directory.is_dir():
            previous = max(
                (p.stat().st_mtime for p in self.directory.glob("*.json") if not p.is_symlink()),
                default=0,
            )
            self.next_at = time.monotonic() + max(0, interval - max(0, time.time() - previous))
        self.latest: tuple[float, bytes, dict[str, Any]] | None = None
        self.event = asyncio.Event()
        self.max_bytes = 2 * 1024**3
        self.min_free = 1024**3
        self.daily_limit = daily_limit
        self.quota = (
            SamplingQuota(
                self.directory,
                state_dir or self.directory.parent / f".{self.directory.name}-state",
                daily_limit,
            )
            if self.directory
            else None
        )

    def _quota_status(self) -> dict[str, Any]:
        moment = datetime.now().astimezone()
        if self.quota:
            return self.quota.snapshot(moment)
        return {
            "daily_saved": 0,
            "daily_limit": self.daily_limit,
            "day": moment.astimezone(SINGAPORE).date().isoformat(),
            "timezone": SINGAPORE.key,
        }

    def _apply_quota(self) -> None:
        if not self.quota:
            return
        current = self._quota_status()
        if (
            isinstance(current["daily_saved"], int)
            and current["daily_saved"] >= current["daily_limit"]
        ):
            self.state, self.error = "daily_limit", ""
        elif self.quota.error:
            self.state, self.error = "paused", self.quota.error
        elif self.state == "daily_limit":
            self.state, self.error = "waiting", ""

    def _refresh_quota(self) -> None:
        if not self.quota:
            return
        try:
            self.quota.refresh(datetime.now().astimezone())
        except (OSError, ValueError) as error:
            self.quota.error = str(error)
            self._apply_quota()
            return
        remaining = max(0, self.interval - max(0, time.time() - self.quota.last_saved_epoch))
        self.next_at = max(self.next_at, time.monotonic() + remaining)
        if self.state == "paused" and self.error:
            self.state, self.error = "waiting", ""
        self._apply_quota()

    async def refresh_quota(self) -> None:
        await asyncio.to_thread(self._refresh_quota)

    async def can_capture(self) -> bool:
        await self.refresh_quota()
        if not self.quota:
            return True
        current = self._quota_status()
        return (
            self.quota.initialized
            and not self.quota.error
            and current["daily_saved"] < current["daily_limit"]
        )

    def submit(self, jpeg: bytes, metadata: dict[str, Any]) -> None:
        if self.directory:
            self._apply_quota()
            if self.state == "daily_limit" or (self.quota and self.quota.error):
                self.latest = None
                self.event.clear()
                return
            if not self.has_person(metadata):
                self.latest = None
                self.event.clear()
                if self.state != "paused":
                    self.state = "waiting_person"
                return
            self.latest = (time.monotonic(), jpeg, metadata)
            self.event.set()

    @staticmethod
    def has_person(metadata: dict[str, Any]) -> bool:
        pose = metadata.get("pose", {})
        return bool(
            (pose.get("state") == "active" and pose.get("people"))
            or (metadata.get("recognition") == "active" and metadata.get("faces"))
        )

    def status(self) -> dict[str, Any]:
        self._apply_quota()
        return {
            "state": self.state,
            "saved": self.saved,
            "last_saved": self.last_saved,
            "interval_seconds": self.interval,
            "error": self.error,
            **self._quota_status(),
        }

    def save(self, jpeg: bytes, metadata: dict[str, Any]) -> str:
        assert self.directory and self.quota
        if not self.has_person(metadata):
            raise ValueError("未检测到人，不保存审核照片")
        if not 1 <= len(jpeg) <= MAX_JPEG:
            raise ValueError("审核原片大小超出限制")
        moment = datetime.now().astimezone()
        stamp = moment.strftime("%Y%m%d-%H%M%S-%f")
        try:
            with self.quota.slot(moment, stamp):
                return self._save(jpeg, metadata, moment, stamp)
        finally:
            self._apply_quota()

    def _save(self, jpeg: bytes, metadata: dict[str, Any], moment: datetime, stamp: str) -> str:
        assert self.directory
        root = self.directory
        if root.is_symlink():
            raise ValueError("审核目录不可为符号链接")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        originals = root / "originals"
        if originals.is_symlink():
            raise ValueError("审核原片目录不可为符号链接")
        originals.mkdir(mode=0o700, exist_ok=True)
        record = {
            **metadata,
            "sampled_at": moment.isoformat(),
            "storage": "mini-local-only",
            "original_available": True,
            "original_image": f"originals/{stamp}.jpg",
        }
        with Image.open(io.BytesIO(jpeg)) as source:
            if source.size != (640, 480):
                raise ValueError("unexpected review frame dimensions")
            image = source.convert("RGB")
        # Add a separate caption area so labels never cover joints.
        people = metadata.get("pose", {}).get("people", [])
        canvas = Image.new("RGB", (640, 480 + 85 + 32 * max(1, len(people))), "#132a23")
        canvas.paste(image)
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.truetype(self.font, 19) if self.font else ImageFont.load_default(size=18)
        for person in people:
            points = person["keypoints"]
            for a, b in CONNECTIONS:
                if points[a][3] >= 0.6 and points[b][3] >= 0.6:
                    draw.line(
                        (
                            points[a][0] * 640,
                            points[a][1] * 480,
                            points[b][0] * 640,
                            points[b][1] * 480,
                        ),
                        fill="#40e0d0",
                        width=3,
                    )
            for point in points:
                if point[3] >= 0.6 and 0 <= point[0] <= 1 and 0 <= point[1] <= 1:
                    x, y = point[0] * 640, point[1] * 480
                    draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill="#ffd47c")
            nose = points[0]
            if nose[3] >= 0.6:
                draw.text(
                    (max(0, min(610, nose[0] * 640)), max(0, min(450, nose[1] * 480))),
                    f"#{person['track_id']}",
                    font=font,
                    fill="#ffd47c",
                    stroke_width=2,
                    stroke_fill="black",
                )
        for face in metadata.get("faces", []):
            draw.rectangle(tuple(face["bbox"]), outline="#c5f58a", width=2)
            draw.text(
                (max(0, face["bbox"][0]), max(0, face["bbox"][1] - 24)),
                face["label"],
                font=font,
                fill="#c5f58a",
                stroke_width=2,
                stroke_fill="black",
            )
        draw.text(
            (10, 490),
            moment.strftime("%Y-%m-%d %H:%M:%S %Z") + f"  帧 {metadata['sequence']}",
            font=font,
            fill="white",
        )
        pose_state = metadata.get("pose", {}).get("state", "disabled")
        draw.text(
            (10, 518),
            f"姿态：{pose_state}  人脸：{metadata.get('recognition', 'unknown')}",
            font=font,
            fill="white",
        )
        lines = [
            f"#{p['track_id']} {p['identity']} · {p['posture_label']} · {p['action_label']}"
            for p in people
        ]
        for i, text in enumerate(lines or ["检测到人脸 / 人体姿态无法判断"]):
            draw.text((10, 550 + i * 32), text, font=font, fill="#ffd47c")
        output = io.BytesIO()
        canvas.save(output, format="JPEG", quality=90)
        contents = (
            (originals, f"{stamp}.jpg", jpeg),
            (root, f"{stamp}.jpg", output.getvalue()),
            (root, f"{stamp}.json", json.dumps(record, ensure_ascii=False, indent=2).encode()),
        )
        total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        pending_bytes = sum(len(content) for _, _, content in contents)
        if (
            total + pending_bytes > self.max_bytes
            or shutil.disk_usage(root).free - pending_bytes < self.min_free
        ):
            raise OSError("审核空间达到上限，已暂停保存并保留已有照片")
        self._write_sample(contents)
        return moment.isoformat()

    @staticmethod
    def _write_sample(contents: tuple[tuple[Path, str, bytes], ...]) -> None:
        """JSON commits last; failed saves cannot leave a half-published sample."""
        with contextlib.ExitStack() as stack:
            directories: dict[Path, int] = {}
            for directory, _, _ in contents:
                if directory not in directories:
                    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    stack.callback(os.close, fd)
                    os.fchmod(fd, 0o700)
                    directories[directory] = fd
            files = [
                (directories[directory], name, content) for directory, name, content in contents
            ]
            # Reject collisions and symlinks before writing anything. Preserve all old samples.
            for directory_fd, name, _ in files:
                try:
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError("审核照片已存在，不覆盖已有样本")
            temporary_files: list[tuple[int, str]] = []
            committed_files: list[tuple[int, str]] = []
            try:
                for directory_fd, name, content in files:
                    temporary = f".{name}.tmp"
                    fd = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    temporary_files.append((directory_fd, temporary))
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                for directory_fd, name, _ in files:
                    os.replace(
                        f".{name}.tmp", name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
                    )
                    committed_files.append((directory_fd, name))
            except BaseException:
                for directory_fd, name in committed_files:
                    with contextlib.suppress(OSError):
                        os.unlink(name, dir_fd=directory_fd)
                raise
            finally:
                for directory_fd, temporary in temporary_files:
                    with contextlib.suppress(OSError):
                        os.unlink(temporary, dir_fd=directory_fd)

    async def run(self) -> None:
        while True:
            await self.event.wait()
            self.event.clear()
            item = self.latest
            now = time.monotonic()
            if (
                not item
                or now < self.next_at
                or now - item[0] > 1.5
                or not self.has_person(item[2])
            ):
                continue
            self.next_at = now + self.interval
            try:
                self.last_saved = await asyncio.to_thread(self.save, item[1], item[2])
                self.saved += 1
                self.state, self.error = "active", ""
                self._apply_quota()
            except DailyLimitReached:
                self.state, self.error = "daily_limit", ""
            except (OSError, ValueError) as error:
                self.state, self.error = "paused", str(error)

    async def consume(self, packet: bytes) -> None:
        size = struct.unpack_from("!I", packet)[0]
        self.submit(packet[4 + size :], json.loads(packet[4 : 4 + size]))
