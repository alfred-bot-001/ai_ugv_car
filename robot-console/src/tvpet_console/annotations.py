"""Private human labels, immutable model evidence and a portable training manifest."""

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .photo_batches import (
    BatchDelete,
    BatchSelection,
    delete_labels,
    export_zip,
    matching_people,
    recover_deletions,
)

SAMPLE_ID = re.compile(r"\d{8}-\d{6}-\d{6}\Z")
PERSON_ID = re.compile(r"(?:pose-\d{1,2}|face-\d{1,2}|manual-[A-Za-z0-9_-]{1,25})\Z")
JSON_LIMIT = 1024 * 1024
IMAGE_LIMIT = 8 * 1024 * 1024
MANIFEST_LIMIT = 32 * 1024 * 1024
POSTURES = {
    "standing": "站着",
    "sitting": "坐着",
    "lying": "躺着",
    "squatting": "蹲着",
    "curled": "蜷缩",
    "unknown": "无法判断",
    "other": "其他",
}
ACTIONS = {
    "none": "无明显动作",
    "raised_hand": "举手",
    "waving": "挥手",
    "greeting": "打招呼",
    "unknown": "无法判断",
    "other": "其他",
}


class AnnotationPerson(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=32)
    identity: str = Field(min_length=1, max_length=64)
    posture: Literal["standing", "sitting", "lying", "squatting", "curled", "unknown", "other"]
    action: Literal["none", "raised_hand", "waving", "greeting", "unknown", "other"]

    @field_validator("id", mode="before")
    @classmethod
    def safe_id(cls, value: Any) -> str:
        if not isinstance(value, str) or not PERSON_ID.fullmatch(value):
            raise ValueError("人物编号无效")
        return value

    @field_validator("identity")
    @classmethod
    def printable_identity(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("人物名称不可包含控制字符")
        return value


class AnnotationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    status: Literal["confirmed", "excluded"]
    people: list[AnnotationPerson] = Field(default_factory=list, max_length=10)
    notes: str = Field(default="", max_length=1000)

    @field_validator("people")
    @classmethod
    def unique_people(cls, value: list[AnnotationPerson]) -> list[AnnotationPerson]:
        if len({person.id for person in value}) != len(value):
            raise ValueError("人物编号不可重复")
        return value


class AnnotationConflict(ValueError):
    """Another editor already saved a newer human revision."""


class _AnnotationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)
    status: Literal["confirmed", "excluded"]
    people: list[AnnotationPerson] = Field(max_length=10)
    notes: str = Field(max_length=1000)
    updated_at: str = Field(max_length=80)

    @field_validator("people")
    @classmethod
    def unique_people(cls, value: list[AnnotationPerson]) -> list[AnnotationPerson]:
        return AnnotationUpdate.unique_people(value)


def _safe_path(path: Path) -> None:
    """Check every existing component, including trusted configured roots."""
    for part in [*reversed(path.parents), path]:
        try:
            mode = part.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError("照片目录与文件不可使用符号链接")


def _private_directory(path: Path) -> None:
    _safe_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError("照片数据目录无效")
    path.chmod(0o700)


def _read(path: Path, limit: int) -> bytes:
    _safe_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("照片文件格式或大小无效")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("照片文件超过大小限制")
    return content


def _json(path: Path) -> dict[str, Any]:
    try:
        record = json.loads(_read(path, JSON_LIMIT))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("照片信息损坏") from error
    if not isinstance(record, dict):
        raise ValueError("照片信息无效")
    return record


def _write(path: Path, content: bytes, *, immutable: bool = False) -> None:
    _safe_path(path)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            # A hard link creates the destination only if it does not exist.
            os.link(temporary, path, follow_symlinks=False)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _encoded(record: dict[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2).encode()


def _text(value: Any, limit: int, fallback: str = "") -> str:
    return value[:limit] if isinstance(value, str) else fallback


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    numbers = [_number(point) for point in value]
    if any(point is None or abs(point) > 8192 for point in numbers):
        return None
    result = [float(point) for point in numbers if point is not None]
    return result if result[2] > result[0] and result[3] > result[1] else None


def _points(value: Any) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != 33:
        return []
    result: list[list[float]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 4:
            return []
        numbers = [_number(coordinate) for coordinate in point]
        if any(coordinate is None or abs(coordinate) > 10 for coordinate in numbers):
            return []
        converted = [float(coordinate) for coordinate in numbers if coordinate is not None]
        if not 0 <= converted[3] <= 1:
            return []
        result.append(converted)
    return result


def _predictions(record: dict[str, Any]) -> list[dict[str, Any]]:
    pose = record.get("pose", {})
    people = pose.get("people", []) if isinstance(pose, dict) else []
    faces = record.get("faces", [])
    people = (
        [person for person in people[:3] if isinstance(person, dict)]
        if isinstance(people, list)
        else []
    )
    faces = (
        [face for face in faces[:16] if isinstance(face, dict)] if isinstance(faces, list) else []
    )
    boxes = [_bbox(face.get("bbox")) for face in faces]
    keypoints = [_points(person.get("keypoints")) for person in people]
    matches: list[list[int]] = []
    for points in keypoints:
        candidates: list[int] = []
        if points and points[0][3] >= 0.6:
            x, y = points[0][0] * 640, points[0][1] * 480
            candidates = [
                i
                for i, box in enumerate(boxes)
                if box and box[0] <= x <= box[2] and box[1] <= y <= box[3]
            ]
        matches.append(candidates)
    result: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for index, person in enumerate(people):
        candidates = matches[index]
        face_index = (
            candidates[0]
            if len(candidates) == 1 and sum(candidates[0] in other for other in matches) == 1
            else None
        )
        face = faces[face_index] if face_index is not None else {}
        if face_index is not None:
            consumed.add(face_index)
        posture = _text(person.get("posture"), 20, "unknown")
        action = _text(person.get("action"), 20, "unknown")
        posture = posture if posture in POSTURES else "unknown"
        action = action if action in ACTIONS else "unknown"
        result.append(
            {
                "id": f"pose-{index}",
                "identity": _text(face.get("label"), 64, "人物未确认"),
                "posture": posture,
                "posture_label": POSTURES[posture],
                "action": action,
                "action_label": ACTIONS[action],
                "score": _number(person.get("score")),
                "similarity": _number(face.get("similarity")),
                "bbox": boxes[face_index] if face_index is not None else None,
                "keypoints": keypoints[index],
                "track_id": person.get("track_id")
                if isinstance(person.get("track_id"), int)
                else None,
            }
        )
    for index, face in enumerate(faces):
        if index not in consumed:
            result.append(
                {
                    "id": f"face-{index}",
                    "identity": _text(face.get("label"), 64, "人物未确认"),
                    "posture": "unknown",
                    "posture_label": POSTURES["unknown"],
                    "action": "unknown",
                    "action_label": ACTIONS["unknown"],
                    "score": None,
                    "similarity": _number(face.get("similarity")),
                    "bbox": boxes[index],
                    "keypoints": [],
                    "track_id": None,
                }
            )
    return result


class AnnotationLibrary:
    """All blocking work belongs in asyncio.to_thread at the HTTP boundary."""

    def __init__(self, sample_dir: str, data_dir: Path) -> None:
        self.sample_dir = Path(sample_dir).absolute() if sample_dir else None
        self.root = data_dir.absolute() / "photo-labels"
        self.samples = self.root / "samples"
        _private_directory(self.root)
        _private_directory(self.samples)
        self.lock = threading.RLock()
        _safe_path(self.root / ".lock")
        descriptor = os.open(self.root / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        with self._locked():
            pass

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self.lock:
            _safe_path(self.root / ".lock")
            descriptor = os.open(self.root / ".lock", os.O_WRONLY | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                # A failed rollback must never expose an uncertain revision to this process.
                # Recovery runs before every read/write; continuing storage faults block access.
                recover_deletions(self)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @staticmethod
    def _validate_id(sample_id: str) -> None:
        if not SAMPLE_ID.fullmatch(sample_id):
            raise ValueError("照片编号无效")
        try:
            datetime.strptime(sample_id, "%Y%m%d-%H%M%S-%f")
        except ValueError as error:
            raise ValueError("照片编号无效") from error

    def _locations(self, sample_id: str) -> tuple[Path, Path, Path]:
        self._validate_id(sample_id)
        if self._is_deleted(sample_id):
            raise FileNotFoundError("照片已移入本机回收目录")
        bundle = self.samples / sample_id
        _safe_path(bundle)
        if bundle.is_dir():
            return bundle / "model.json", bundle / "annotated.jpg", bundle / "original.jpg"
        if self.sample_dir is None:
            raise FileNotFoundError("照片不存在")
        _safe_path(self.sample_dir)
        return (
            self.sample_dir / f"{sample_id}.json",
            self.sample_dir / f"{sample_id}.jpg",
            self.sample_dir / "originals" / f"{sample_id}.jpg",
        )

    def _ids(self) -> list[str]:
        result: set[str] = set()
        _safe_path(self.samples)
        for path in self.samples.iterdir():
            if SAMPLE_ID.fullmatch(path.name) and not path.is_symlink() and path.is_dir():
                result.add(path.name)
        if self.sample_dir is not None:
            _safe_path(self.sample_dir)
            if self.sample_dir.is_dir():
                for path in self.sample_dir.glob("*.json"):
                    if SAMPLE_ID.fullmatch(path.stem) and not path.is_symlink():
                        picture = path.with_suffix(".jpg")
                        if picture.is_file() and not picture.is_symlink():
                            result.add(path.stem)
        active = []
        for sample_id in sorted(result, reverse=True):
            try:
                if not self._is_deleted(sample_id):
                    active.append(sample_id)
            except (OSError, ValueError):
                continue
        return active

    def _is_deleted(self, sample_id: str) -> bool:
        marker = self.root / "deleted" / f"{sample_id}.json"
        _safe_path(marker)
        if not marker.is_file():
            return False
        record = _json(marker)
        revision = record.get("revision")
        if (
            record.get("id") != sample_id
            or record.get("scope") != "photos"
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            raise ValueError("照片删除记录无效")
        return True

    def _annotation(self, sample_id: str) -> dict[str, Any] | None:
        bundle = self.samples / sample_id
        _safe_path(bundle)
        if not bundle.is_dir():
            return None
        history = bundle / "history"
        _safe_path(history)
        revisions = (
            sorted(
                path
                for path in history.glob("revision-*.json")
                if re.fullmatch(r"revision-\d{8}\.json", path.name)
            )
            if history.is_dir()
            else []
        )
        # History is the commit record; recover a save interrupted before latest/manifest replace.
        if not revisions:
            return None
        return _AnnotationRecord.model_validate(_json(revisions[-1])).model_dump()

    def _revision(self, sample_id: str, annotation: dict[str, Any] | None) -> int:
        revision = annotation["revision"] if annotation else 0
        tombstone = self.samples / sample_id / "tombstone.json"
        _safe_path(tombstone)
        if tombstone.is_file():
            previous = _json(tombstone).get("revision")
            if not isinstance(previous, int) or isinstance(previous, bool) or previous < 1:
                raise ValueError("照片删除记录无效")
            revision = max(revision, previous)
        return int(revision)

    def _detail(self, sample_id: str) -> dict[str, Any]:
        metadata, picture, original = self._locations(sample_id)
        _safe_path(picture)
        if not picture.is_file() or picture.stat().st_size > IMAGE_LIMIT:
            raise FileNotFoundError("照片不存在")
        record = _json(metadata)
        _safe_path(original)
        original_available = original.is_file() and original.stat().st_size <= IMAGE_LIMIT
        pose = record.get("pose", {})
        if not isinstance(pose, dict):
            pose = {}
        annotation = self._annotation(sample_id)
        return {
            "id": sample_id,
            "sampled_at": _text(record.get("sampled_at"), 80),
            "sequence": record.get("sequence") if isinstance(record.get("sequence"), int) else None,
            "original_available": original_available,
            "image_url": f"/api/v1/photos/{sample_id}/image?kind=annotated",
            "original_url": f"/api/v1/photos/{sample_id}/image?kind=original"
            if original_available
            else None,
            "predictions": _predictions(record),
            "annotation": annotation,
            "revision": self._revision(sample_id, annotation),
            "model": _text(pose.get("model"), 80),
            "rules": _text(pose.get("rules"), 80),
        }

    def detail(self, sample_id: str) -> dict[str, Any]:
        with self._locked():
            return self._detail(sample_id)

    def list_samples(
        self,
        page: int = 1,
        limit: int = 24,
        status: str = "all",
        q: str = "",
        identity: str = "",
        posture: str = "",
        label_source: str = "effective",
    ) -> dict[str, Any]:
        if (
            page < 1
            or not 1 <= limit <= 100
            or status not in {"all", "pending", "confirmed", "excluded"}
        ):
            raise ValueError("照片列表参数无效")
        if len(q) > 100:
            raise ValueError("搜索文字过长")
        if len(identity) > 64 or posture not in {"", *POSTURES}:
            raise ValueError("人物或姿态筛选无效")
        if label_source not in {"effective", "manual", "prediction"}:
            raise ValueError("标注来源无效")
        with self._locked():
            items = []
            total = 0
            start = (page - 1) * limit
            stats = {"total": 0, "pending": 0, "confirmed": 0, "excluded": 0, "originals": 0}
            categories: dict[str, int] = {}
            for sample_id in self._ids():
                try:
                    item = self._detail(sample_id)
                except (OSError, ValueError):
                    continue
                current_status = item["annotation"]["status"] if item["annotation"] else "pending"
                if current_status not in {"pending", "confirmed", "excluded"}:
                    continue
                stats["total"] += 1
                stats[current_status] += 1
                stats["originals"] += int(item["original_available"])
                if current_status == "confirmed":
                    for name in {person["identity"] for person in item["annotation"]["people"]}:
                        categories[name] = categories.get(name, 0) + 1
                searchable = [sample_id, item["sampled_at"]]
                for person in item["predictions"]:
                    searchable.extend(
                        [person["identity"], person["posture_label"], person["action_label"]]
                    )
                if item["annotation"]:
                    searchable.append(item["annotation"].get("notes", ""))
                    for person in item["annotation"].get("people", []):
                        searchable.extend(
                            [
                                person["identity"],
                                POSTURES.get(person["posture"], ""),
                                ACTIONS.get(person["action"], ""),
                            ]
                        )
                if status != "all" and current_status != status:
                    continue
                if label_source == "manual":
                    people = item["annotation"]["people"] if current_status == "confirmed" else []
                    if current_status != "confirmed":
                        continue
                elif label_source == "prediction":
                    people = item["predictions"]
                else:
                    people = (
                        item["annotation"]["people"] if item["annotation"] else item["predictions"]
                    )
                if (identity or posture) and not matching_people(people, identity, posture):
                    continue
                if q.casefold() not in " ".join(searchable).casefold():
                    continue
                if start <= total < start + limit:
                    # Lists need labels only. Full geometry is fetched for the selected detail.
                    for person in item["predictions"]:
                        person["keypoints"] = []
                    items.append(item)
                total += 1
            return {
                "items": items,
                "total": total,
                "page": page,
                "limit": limit,
                "pages": math.ceil(total / limit),
                "stats": stats,
                "categories": [
                    {"identity": name, "count": count} for name, count in sorted(categories.items())
                ],
            }

    def image(self, sample_id: str, kind: str = "annotated") -> bytes:
        if kind not in {"annotated", "original"}:
            raise ValueError("照片类型无效")
        with self._locked():
            _, picture, original = self._locations(sample_id)
            content = _read(original if kind == "original" else picture, IMAGE_LIMIT)
            if not content.startswith(b"\xff\xd8"):
                raise ValueError("照片不是 JPEG 格式")
            return content

    def _preserve(self, sample_id: str) -> None:
        destination = self.samples / sample_id
        _safe_path(destination)
        if destination.is_dir():
            return
        metadata, picture, original = self._locations(sample_id)
        contents = {
            "model.json": _read(metadata, JSON_LIMIT),
            "annotated.jpg": _read(picture, IMAGE_LIMIT),
        }
        _safe_path(original)
        if original.is_file():
            contents["original.jpg"] = _read(original, IMAGE_LIMIT)
        if any(
            not content.startswith(b"\xff\xd8")
            for name, content in contents.items()
            if name.endswith(".jpg")
        ):
            raise ValueError("照片不是 JPEG 格式")
        staging = self.samples / f".{sample_id}.{uuid.uuid4().hex}.tmp"
        _private_directory(staging)
        try:
            for filename, content in contents.items():
                _write(staging / filename, content, immutable=True)
            _write(
                staging / "provenance.json",
                _encoded(
                    {
                        "sample_id": sample_id,
                        "preserved_at": datetime.now(UTC).isoformat(),
                        "sha256": {
                            name: hashlib.sha256(data).hexdigest()
                            for name, data in contents.items()
                        },
                        "original_available": "original.jpg" in contents,
                    }
                ),
                immutable=True,
            )
            _private_directory(staging / "history")
            staging.rename(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def save(self, sample_id: str, body: AnnotationUpdate) -> dict[str, Any]:
        with self._locked():
            detail = self._detail(sample_id)
            current = detail["revision"]
            if body.expected_revision != current:
                raise AnnotationConflict("这张照片已被修改，请刷新后再保存")
            predicted_ids = {person["id"] for person in detail["predictions"]}
            if any(
                person.id not in predicted_ids and not person.id.startswith("manual-")
                for person in body.people
            ):
                raise ValueError("人物编号不属于这张照片")
            if body.status == "confirmed" and not body.people:
                raise ValueError("确认标注时至少填写一位人物，无人照片请选择排除")
            self._preserve(sample_id)
            _private_directory(self.samples / sample_id / "history")
            annotation = {
                "revision": current + 1,
                "status": body.status,
                "people": [person.model_dump() for person in body.people],
                "notes": body.notes,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            bundle = self.samples / sample_id
            _write(
                bundle / "history" / f"revision-{current + 1:08d}.json",
                _encoded(annotation),
                immutable=True,
            )
            _write(bundle / "annotation.json", _encoded(annotation))
            _write(self.root / "dataset.jsonl", self._manifest())
            return self._detail(sample_id)

    def _manifest(self) -> bytes:
        lines: list[bytes] = []
        size = 0
        _safe_path(self.samples)
        for bundle in sorted(self.samples.iterdir(), reverse=True):
            sample_id = bundle.name
            if not SAMPLE_ID.fullmatch(sample_id) or not bundle.is_dir():
                continue
            try:
                detail = self._detail(sample_id)
                annotation = detail["annotation"]
                if not annotation or annotation["status"] != "confirmed":
                    continue
                provenance = _json(bundle / "provenance.json")
                model_record = _json(bundle / "model.json")
                predictions = {person["id"]: person for person in detail["predictions"]}
                people = []
                for person in annotation["people"]:
                    evidence = predictions.get(person["id"], {})
                    people.append(
                        {
                            **person,
                            "bbox": evidence.get("bbox"),
                            "keypoints": evidence.get("keypoints", []),
                            "track_id": evidence.get("track_id"),
                        }
                    )
                record = {
                    "schema": "tvpet-photo-labels.v1",
                    "id": sample_id,
                    "sampled_at": detail["sampled_at"],
                    "sequence": detail["sequence"],
                    "timestamp_ns": model_record.get("timestamp_ns")
                    if isinstance(model_record.get("timestamp_ns"), int)
                    else None,
                    "model_metadata": f"samples/{sample_id}/model.json",
                    "original_available": detail["original_available"],
                    "images": {
                        "annotated": f"samples/{sample_id}/annotated.jpg",
                        "original": f"samples/{sample_id}/original.jpg"
                        if detail["original_available"]
                        else None,
                    },
                    "model": detail["model"],
                    "rules": detail["rules"],
                    "predictions": detail["predictions"],
                    "annotation": {**annotation, "people": people},
                    "sha256": provenance["sha256"],
                }
                line = (
                    json.dumps(
                        record, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    ).encode()
                    + b"\n"
                )
                size += len(line)
                if size > MANIFEST_LIMIT:
                    raise OSError("训练清单超过 32 MiB，请分批导出")
                lines.append(line)
            except (ValueError, KeyError, FileNotFoundError):
                continue
        return b"".join(lines)

    def manifest(self) -> bytes:
        with self._locked():
            # Immutable revisions keep exports current after an interrupted save.
            result = self._manifest()
            _write(self.root / "dataset.jsonl", result)
            return result

    def export_zip(self, selection: BatchSelection) -> Path:
        return export_zip(self, selection)

    def delete_labels(self, body: BatchDelete) -> dict[str, Any]:
        return delete_labels(self, body)
