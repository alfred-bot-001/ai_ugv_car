"""Bounded local training exports and recoverable, transactional label deletion."""

import json
import os
import re
import stat
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from .annotations import AnnotationLibrary

EXPORT_LIMIT = 256 * 1024 * 1024
MAX_SAMPLES = 200
PostureFilter = Literal[
    "", "standing", "sitting", "lying", "squatting", "curled", "unknown", "other"
]


def _sample_id(value: str) -> str:
    if not re.fullmatch(r"\d{8}-\d{6}-\d{6}", value):
        raise ValueError("照片编号无效")
    try:
        datetime.strptime(value, "%Y%m%d-%H%M%S-%f")
    except ValueError as error:
        raise ValueError("照片编号无效") from error
    return value


class BatchSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ids: list[str] = Field(default_factory=list, max_length=MAX_SAMPLES)
    identity: str = Field(default="", max_length=64)
    posture: PostureFilter = ""
    label_source: Literal["manual"] = "manual"

    @field_validator("ids")
    @classmethod
    def valid_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("照片编号不可重复")
        return [_sample_id(value) for value in values]

    @field_validator("identity")
    @classmethod
    def printable_identity(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("人物名称不可包含控制字符")
        return value.strip()


class DeleteItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    expected_revision: int = Field(ge=0)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return _sample_id(value)


class BatchDelete(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[DeleteItem] = Field(min_length=1, max_length=MAX_SAMPLES)
    scope: Literal["labels", "photos"] = "labels"

    @field_validator("items")
    @classmethod
    def unique_items(cls, values: list[DeleteItem]) -> list[DeleteItem]:
        if len({value.id for value in values}) != len(values):
            raise ValueError("照片编号不可重复")
        return values


def matching_people(
    people: list[dict[str, Any]], identity: str = "", posture: str = ""
) -> list[str]:
    """Identity and posture must both belong to the same person in the picture."""
    return [
        person["id"]
        for person in people
        if (not identity or person["identity"] == identity)
        and (not posture or person["posture"] == posture)
    ]


def _selected(library: "AnnotationLibrary", selection: BatchSelection) -> list[dict[str, Any]]:
    result = []
    for sample_id in selection.ids or library._ids():
        try:
            detail = library._detail(sample_id)
        except (OSError, ValueError):
            if selection.ids:
                raise
            continue
        annotation = detail["annotation"]
        if not annotation or annotation["status"] != "confirmed":
            if selection.ids:
                raise ValueError("批量导出仅支持已人工确认的照片")
            continue
        matched = matching_people(annotation["people"], selection.identity, selection.posture)
        if not matched:
            continue
        result.append({**detail, "matched_person_ids": matched})
        if len(result) > MAX_SAMPLES:
            raise ValueError("一次最多导出 200 张照片，请缩小筛选范围")
    if not result:
        raise ValueError("没有符合条件的已确认照片")
    return result


def _copy_to_zip(archive: zipfile.ZipFile, source: Path, target: str, limit: int) -> int:
    from .annotations import _safe_path

    _safe_path(source)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("导出来源文件格式或大小无效")
        total = 0
        with archive.open(target, "w") as output:
            while content := stream.read(min(1024 * 1024, limit - total + 1)):
                total += len(content)
                if total > limit:
                    raise ValueError("导出文件超过大小限制")
                output.write(content)
        return total


def _training_record(library: "AnnotationLibrary", detail: dict[str, Any]) -> dict[str, Any]:
    from .annotations import _json

    sample_id = detail["id"]
    bundle = library.samples / sample_id
    source = _json(bundle / "model.json")
    provenance = _json(bundle / "provenance.json")
    predictions = {person["id"]: person for person in detail["predictions"]}
    people = []
    for person in detail["annotation"]["people"]:
        prediction = predictions.get(person["id"], {})
        people.append(
            {
                **person,
                "bbox": prediction.get("bbox"),
                "keypoints": prediction.get("keypoints", []),
                "track_id": prediction.get("track_id"),
            }
        )
    return {
        "schema": "tvpet-photo-labels.v1",
        "id": sample_id,
        "sampled_at": detail["sampled_at"],
        "sequence": detail["sequence"],
        "timestamp_ns": source.get("timestamp_ns")
        if isinstance(source.get("timestamp_ns"), int)
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
        "annotation": {**detail["annotation"], "people": people},
        "matched_person_ids": detail["matched_person_ids"],
        "sha256": provenance["sha256"],
    }


def export_zip(library: "AnnotationLibrary", selection: BatchSelection) -> Path:
    from .annotations import IMAGE_LIMIT, JSON_LIMIT, _encoded, _private_directory, _safe_path

    with library._locked():
        details = _selected(library, selection)
        plans: list[tuple[Path, str, int]] = []
        labels: list[tuple[str, bytes]] = []
        records = []
        total = 0
        for detail in details:
            sample_id = detail["id"]
            bundle = library.samples / sample_id
            for name, limit in [
                ("annotated.jpg", IMAGE_LIMIT),
                ("model.json", JSON_LIMIT),
                ("provenance.json", JSON_LIMIT),
                *([("original.jpg", IMAGE_LIMIT)] if detail["original_available"] else []),
            ]:
                source = bundle / name
                _safe_path(source)
                info = source.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    raise ValueError("导出来源文件格式或大小无效")
                total += info.st_size
                plans.append((source, f"samples/{sample_id}/{name}", limit))
            latest = _encoded(detail["annotation"])
            labels.append((f"samples/{sample_id}/annotation.json", latest))
            total += len(latest)
            records.append(_training_record(library, detail))
        manifest = b"".join(
            json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            + b"\n"
            for record in records
        )
        total += len(manifest)
        if total > EXPORT_LIMIT:
            raise ValueError("导出内容超过 256 MiB，请分批导出")
        directory = library.root / "exports"
        _private_directory(directory)
        descriptor, filename = tempfile.mkstemp(
            prefix="photo-training-", suffix=".zip", dir=directory
        )
        destination = Path(filename)
        try:
            copied = len(manifest) + sum(len(content) for _, content in labels)
            with os.fdopen(descriptor, "wb") as stream, zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("dataset.jsonl", manifest)
                for name, content in labels:
                    archive.writestr(name, content)
                for source, target, limit in plans:
                    copied += _copy_to_zip(archive, source, target, limit)
                    if copied > EXPORT_LIMIT:
                        raise ValueError("导出内容超过 256 MiB，请分批导出")
            return destination
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def _rollback(library: "AnnotationLibrary", batch: Path, transaction: dict[str, Any]) -> None:
    from .annotations import _encoded, _private_directory, _safe_path, _write

    if transaction.get("scope") == "photos":
        _rollback_photos(library, batch, transaction)
        return
    for entry in reversed(transaction["items"]):
        sample_id = _sample_id(entry["id"])
        bundle = library.samples / sample_id
        backup = batch / sample_id
        _safe_path(bundle)
        _safe_path(backup)
        for name in ("annotation.json", "history"):
            source = backup / name
            target = bundle / name
            _safe_path(source)
            _safe_path(target)
            if source.exists():
                if target.exists():
                    raise ValueError("删除恢复遇到已有标签，停止覆盖")
                _private_directory(bundle)
                source.rename(target)
        tombstone = bundle / "tombstone.json"
        _safe_path(tombstone)
        previous = entry.get("previous_tombstone")
        if previous is None:
            tombstone.unlink(missing_ok=True)
        else:
            _write(tombstone, _encoded(previous))
    _write(library.root / "dataset.jsonl", library._manifest())
    _write(batch / "transaction.json", _encoded({**transaction, "status": "rolled_back"}))


def recover_deletions(library: "AnnotationLibrary") -> None:
    from .annotations import _json, _safe_path

    trash = library.root / "trash"
    _safe_path(trash)
    if not trash.is_dir():
        return
    for batch in trash.iterdir():
        if not re.fullmatch(r"[0-9a-f]{32}", batch.name):
            continue
        _safe_path(batch)
        journal = batch / "transaction.json"
        _safe_path(journal)
        if journal.is_file():
            transaction = _json(journal)
            if transaction.get("status") == "pending":
                _rollback(library, batch, transaction)


def delete_labels(library: "AnnotationLibrary", body: BatchDelete) -> dict[str, Any]:
    from .annotations import (
        AnnotationConflict,
        _encoded,
        _json,
        _private_directory,
        _safe_path,
        _write,
    )

    if body.scope == "photos":
        return _delete_photos(library, body)
    with library._locked():
        _safe_path(library.root / "dataset.jsonl")
        entries = []
        for item in body.items:
            detail = library._detail(item.id)
            if detail["revision"] != item.expected_revision:
                raise AnnotationConflict("所选照片已被修改，请刷新列表后重新选择")
            if detail["annotation"] is None:
                raise ValueError("所选照片尚无人工标注")
            bundle = library.samples / item.id
            for name in ("history", "annotation.json", "tombstone.json"):
                _safe_path(bundle / name)
            history = bundle / "history"
            if not history.is_dir():
                raise ValueError("人工标注历史缺失")
            for path in history.iterdir():
                _safe_path(path)
            previous = (
                _json(bundle / "tombstone.json") if (bundle / "tombstone.json").is_file() else None
            )
            entries.append(
                {"id": item.id, "revision": detail["revision"], "previous_tombstone": previous}
            )
        trash = library.root / "trash"
        _safe_path(trash)
        batch_id = uuid.uuid4().hex
        batch = trash / batch_id
        _private_directory(trash)
        _private_directory(batch)
        transaction = {"status": "pending", "scope": "labels", "items": entries}
        _write(batch / "transaction.json", _encoded(transaction))
        try:
            for entry in entries:
                sample_id = entry["id"]
                bundle = library.samples / sample_id
                backup = batch / sample_id
                _private_directory(backup)
                for name in ("annotation.json", "history"):
                    source = bundle / name
                    if source.exists():
                        source.rename(backup / name)
                _write(
                    bundle / "tombstone.json",
                    _encoded(
                        {
                            "revision": entry["revision"] + 1,
                            "deleted_at": datetime.now(UTC).isoformat(),
                            "batch_id": batch_id,
                        }
                    ),
                )
            _write(library.root / "dataset.jsonl", library._manifest())
            _write(batch / "transaction.json", _encoded({**transaction, "status": "committed"}))
        except BaseException:
            _rollback(library, batch, transaction)
            raise
        return {
            "deleted": len(entries),
            "ids": [entry["id"] for entry in entries],
            "scope": "labels",
            "info": "人工标注已移入本机回收目录，照片和模型结果保留",
        }


def _photo_paths(library: "AnnotationLibrary", sample_id: str) -> dict[str, Path]:
    _sample_id(sample_id)
    result = {"bundle": library.samples / sample_id}
    if library.sample_dir is not None:
        result.update(
            {
                "source-model.json": library.sample_dir / f"{sample_id}.json",
                "source-annotated.jpg": library.sample_dir / f"{sample_id}.jpg",
                "source-original.jpg": library.sample_dir / "originals" / f"{sample_id}.jpg",
            }
        )
    return result


def _file_signature(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _preflight_photos(library: "AnnotationLibrary", body: BatchDelete) -> list[dict[str, Any]]:
    from .annotations import IMAGE_LIMIT, AnnotationConflict, _json, _read, _safe_path

    entries = []
    _safe_path(library.root / "dataset.jsonl")
    _safe_path(library.root / "trash")
    _safe_path(library.root / "deleted")
    for item in body.items:
        detail = library._detail(item.id)
        if detail["revision"] != item.expected_revision:
            raise AnnotationConflict("所选照片已被修改，请刷新列表后重新选择")
        present = {}
        for name, source in _photo_paths(library, item.id).items():
            _safe_path(source)
            if not source.exists():
                continue
            if name == "bundle":
                if not source.is_dir():
                    raise ValueError("照片数据包无效")
                for path in source.rglob("*"):
                    _safe_path(path)
                    if path.is_dir():
                        continue
                    if path.suffix == ".json":
                        _json(path)
                    else:
                        _read(path, IMAGE_LIMIT)
            elif source.suffix == ".json":
                _json(source)
            else:
                content = _read(source, IMAGE_LIMIT)
                if not content.startswith(b"\xff\xd8"):
                    raise ValueError("照片不是 JPEG 格式")
            present[name] = _file_signature(source)
        marker = library.root / "deleted" / f"{item.id}.json"
        _safe_path(marker)
        previous = _json(marker) if marker.is_file() else None
        entries.append(
            {
                "id": item.id,
                "revision": detail["revision"],
                "present": present,
                "previous_deleted": previous,
            }
        )
    return entries


def _rollback_photos(
    library: "AnnotationLibrary", batch: Path, transaction: dict[str, Any]
) -> None:
    from .annotations import _encoded, _private_directory, _safe_path, _write

    expected_source = str(library.sample_dir) if library.sample_dir is not None else None
    if transaction.get("sample_dir") != expected_source:
        raise ValueError("采样目录已变化，请先恢复原目录配置再恢复删除事务")
    plans = []
    # Validate the whole journal before restoring any selected file.
    items = transaction.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_SAMPLES:
        raise ValueError("照片删除事务无效")
    for entry in reversed(items):
        if not isinstance(entry, dict):
            raise ValueError("照片删除事务无效")
        sample_id = _sample_id(entry["id"])
        paths = _photo_paths(library, sample_id)
        for name, destination in paths.items():
            source = batch / sample_id / name
            _safe_path(source)
            _safe_path(destination)
            if source.exists() and destination.exists():
                raise ValueError("照片恢复遇到已有文件，停止覆盖")
        marker = library.root / "deleted" / f"{sample_id}.json"
        _safe_path(marker)
        plans.append((entry, paths, marker))
    for entry, paths, marker in plans:
        for name, destination in paths.items():
            source = batch / entry["id"] / name
            if source.exists():
                _private_directory(destination.parent)
                source.rename(destination)
        previous = entry.get("previous_deleted")
        if previous is None:
            marker.unlink(missing_ok=True)
        else:
            _write(marker, _encoded(previous))
    _write(library.root / "dataset.jsonl", library._manifest())
    _write(batch / "transaction.json", _encoded({**transaction, "status": "rolled_back"}))


def _delete_photos(library: "AnnotationLibrary", body: BatchDelete) -> dict[str, Any]:
    from .annotations import AnnotationConflict, _encoded, _private_directory, _safe_path, _write

    with library._locked():
        entries = _preflight_photos(library, body)
        batch_id = uuid.uuid4().hex
        trash = library.root / "trash"
        batch = trash / batch_id
        _private_directory(trash)
        _private_directory(batch)
        _private_directory(library.root / "deleted")
        transaction = {
            "status": "pending",
            "scope": "photos",
            "items": entries,
            "sample_dir": str(library.sample_dir) if library.sample_dir is not None else None,
        }
        _write(batch / "transaction.json", _encoded(transaction))
        try:
            for entry in entries:
                sample_id = entry["id"]
                backup = batch / sample_id
                _private_directory(backup)
                paths = _photo_paths(library, sample_id)
                for name, signature in entry["present"].items():
                    source = paths[name]
                    _safe_path(source)
                    if not source.exists() or _file_signature(source) != signature:
                        raise AnnotationConflict("所选照片文件已变化，请刷新后重新选择")
                    moved = backup / name
                    source.rename(moved)
                    moved.chmod(0o700 if moved.is_dir() else 0o600)
                    if moved.is_dir():
                        for path in moved.rglob("*"):
                            _safe_path(path)
                            path.chmod(0o700 if path.is_dir() else 0o600)
                _write(
                    library.root / "deleted" / f"{sample_id}.json",
                    _encoded(
                        {
                            "id": sample_id,
                            "scope": "photos",
                            "revision": entry["revision"] + 1,
                            "deleted_at": datetime.now(UTC).isoformat(),
                            "batch_id": batch_id,
                        }
                    ),
                )
            _write(library.root / "dataset.jsonl", library._manifest())
            _write(batch / "transaction.json", _encoded({**transaction, "status": "committed"}))
        except BaseException:
            _rollback_photos(library, batch, transaction)
            raise
        return {
            "deleted": len(entries),
            "ids": [entry["id"] for entry in entries],
            "scope": "photos",
            "info": "照片、原片、模型结果和人工标注已移入本机回收目录",
        }
