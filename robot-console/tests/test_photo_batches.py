"""Person filters, complete portable exports and recoverable bulk label removal."""

import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError
from tvpet_console import annotations, photo_batches
from tvpet_console.annotations import (
    AnnotationConflict,
    AnnotationLibrary,
    AnnotationUpdate,
    BatchDelete,
    BatchSelection,
)


def seed(tmp_path, index=1, *, raw=True, confirmed=True, people=None):
    stamp = f"20261004-1200{index:02d}-123456"
    source = tmp_path / "review"
    source.mkdir(exist_ok=True)
    output = io.BytesIO()
    Image.new("RGB", (640, 480), "green").save(output, "JPEG")
    (source / f"{stamp}.jpg").write_bytes(output.getvalue())
    record = {
        "sampled_at": "2026-10-04T12:00:00+08:00",
        "sequence": index,
        "timestamp_ns": index,
        "pose": {"model": "pose-lite", "rules": "v1", "people": []},
        "faces": [{"bbox": [0, 0, 20, 20], "label": "爸爸", "similarity": 0.6}],
    }
    (source / f"{stamp}.json").write_text(json.dumps(record))
    if raw:
        (source / "originals").mkdir(exist_ok=True)
        original = io.BytesIO()
        Image.new("RGB", (640, 480), "red").save(original, "JPEG")
        (source / "originals" / f"{stamp}.jpg").write_bytes(original.getvalue())
    library = AnnotationLibrary(str(source), tmp_path / "state")
    if confirmed:
        library.save(
            stamp,
            AnnotationUpdate.model_validate(
                {
                    "expected_revision": 0,
                    "status": "confirmed",
                    "people": people
                    or [
                        {
                            "id": "face-0",
                            "identity": "儿子",
                            "posture": "sitting",
                            "action": "none",
                        }
                    ],
                }
            ),
        )
    return library, stamp, source


def deletion(*items, scope="labels"):
    return BatchDelete.model_validate(
        {
            "items": [
                {"id": sample_id, "expected_revision": revision} for sample_id, revision in items
            ],
            "scope": scope,
        }
    )


def test_manual_categories_count_photos_and_person_filter_is_joint(tmp_path):
    people = [
        {"id": "face-0", "identity": "爸爸", "posture": "standing", "action": "none"},
        {"id": "manual-son", "identity": "儿子", "posture": "sitting", "action": "none"},
        {"id": "manual-son2", "identity": "儿子", "posture": "lying", "action": "none"},
    ]
    library, first, _ = seed(tmp_path, people=people)
    _, pending, _ = seed(tmp_path, 2, confirmed=False)
    _, third, _ = seed(tmp_path, 3)
    library.save(
        third,
        AnnotationUpdate.model_validate(
            {
                "expected_revision": 1,
                "status": "excluded",
                "people": [],
            }
        ),
    )
    categories = library.list_samples()["categories"]
    assert categories == [{"identity": "儿子", "count": 1}, {"identity": "爸爸", "count": 1}]
    # Machine "爸爸" on pending data never becomes a manually confirmed category.
    manual = library.list_samples(identity="爸爸", label_source="manual")
    assert [item["id"] for item in manual["items"]] == [first]
    assert library.list_samples(identity="爸爸", posture="sitting")["total"] == 0
    assert (
        library.list_samples(identity="儿子", posture="lying", label_source="manual")["total"] == 1
    )
    assert library.list_samples(identity="爸爸", label_source="prediction")["total"] == 3
    effective = library.list_samples(identity="爸爸")
    assert {item["id"] for item in effective["items"]} == {first, pending}
    assert library.list_samples(posture="standing")["items"][0]["id"] == first


@pytest.mark.parametrize(
    "filters",
    [
        {"identity": "x" * 65},
        {"posture": "running"},
        {"label_source": "broken"},
    ],
)
def test_filter_validation(tmp_path, filters):
    library = AnnotationLibrary("", tmp_path / "state")
    with pytest.raises(ValueError):
        library.list_samples(**filters)


def test_zip_keeps_all_people_and_exact_images_and_marks_missing_original(tmp_path):
    people = [
        {"id": "face-0", "identity": "儿子", "posture": "sitting", "action": "none"},
        {"id": "manual-mom", "identity": "妈妈", "posture": "lying", "action": "none"},
    ]
    library, first, source = seed(tmp_path, people=people)
    _, second, _ = seed(tmp_path, 2, raw=False)
    path = library.export_zip(BatchSelection(identity="儿子", posture="sitting"))
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with zipfile.ZipFile(path) as archive:
        records = [json.loads(line) for line in archive.read("dataset.jsonl").splitlines()]
        by_id = {record["id"]: record for record in records}
        assert set(by_id) == {first, second}
        assert by_id[first]["matched_person_ids"] == ["face-0"]
        assert {person["identity"] for person in by_id[first]["annotation"]["people"]} == {
            "儿子",
            "妈妈",
        }
        assert (
            archive.read(f"samples/{first}/original.jpg")
            == (source / "originals" / f"{first}.jpg").read_bytes()
        )
        assert archive.read(f"samples/{first}/annotated.jpg") == library.image(first)
        assert json.loads(archive.read(f"samples/{first}/annotation.json"))["revision"] == 1
        assert (
            archive.read(f"samples/{first}/model.json") == (source / f"{first}.json").read_bytes()
        )
        assert not by_id[second]["original_available"]
        assert f"samples/{second}/original.jpg" not in archive.namelist()
        assert all(not name.startswith("/") and ".." not in name for name in archive.namelist())
    path.unlink()


def test_zip_selection_requires_confirmation_and_joint_match(tmp_path):
    library, confirmed, _ = seed(tmp_path)
    _, pending, _ = seed(tmp_path, 2, confirmed=False)
    with pytest.raises(ValueError, match="人工确认"):
        library.export_zip(BatchSelection(ids=[confirmed, pending]))
    with pytest.raises(ValueError, match="没有符合"):
        library.export_zip(BatchSelection(identity="爸爸"))
    with pytest.raises(ValueError, match="没有符合"):
        library.export_zip(BatchSelection(identity="儿子", posture="standing"))
    path = library.export_zip(BatchSelection(ids=[confirmed]))
    with zipfile.ZipFile(path) as archive:
        assert len(archive.read("dataset.jsonl").splitlines()) == 1
    path.unlink()


def test_zip_label_uses_committed_history_not_stale_latest_file(tmp_path):
    library, sample_id, _ = seed(tmp_path)
    latest = library.samples / sample_id / "annotation.json"
    latest.write_text('{"revision":999}')
    path = library.export_zip(BatchSelection(ids=[sample_id]))
    with zipfile.ZipFile(path) as archive:
        assert json.loads(archive.read(f"samples/{sample_id}/annotation.json"))["revision"] == 1
    path.unlink()


def test_zip_bounds_and_partial_failure_cleanup(tmp_path, monkeypatch):
    library, _, _ = seed(tmp_path)
    seed(tmp_path, 2)
    monkeypatch.setattr(photo_batches, "MAX_SAMPLES", 1)
    with pytest.raises(ValueError, match="200"):
        library.export_zip(BatchSelection())
    monkeypatch.setattr(photo_batches, "MAX_SAMPLES", 200)
    monkeypatch.setattr(photo_batches, "EXPORT_LIMIT", 10)
    with pytest.raises(ValueError, match="256 MiB"):
        library.export_zip(BatchSelection())
    monkeypatch.setattr(photo_batches, "EXPORT_LIMIT", 256 * 1024 * 1024)

    def failure(*args):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(photo_batches, "_copy_to_zip", failure)
    with pytest.raises(OSError):
        library.export_zip(BatchSelection())
    assert not list((library.root / "exports").glob("*.zip"))


def test_zip_never_uses_person_name_for_archive_paths(tmp_path):
    identity = "爸爸/../../目录"
    people = [{"id": "face-0", "identity": identity, "posture": "standing", "action": "none"}]
    library, _, _ = seed(tmp_path, people=people)
    path = library.export_zip(BatchSelection(identity=identity))
    with zipfile.ZipFile(path) as archive:
        assert all(identity not in name and ".." not in name for name in archive.namelist())
    path.unlink()


def test_symlink_export_or_evidence_rejected(tmp_path):
    library, sample_id, _ = seed(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (library.root / "exports").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        library.export_zip(BatchSelection())
    assert list(outside.iterdir()) == []
    (library.root / "exports").unlink()
    provenance = library.samples / sample_id / "provenance.json"
    provenance.unlink()
    provenance.symlink_to(outside / "private")
    with pytest.raises(ValueError, match="符号链接"):
        library.export_zip(BatchSelection())


def test_bulk_delete_keeps_images_returns_pending_and_prevents_aba(tmp_path):
    library, first, source = seed(tmp_path)
    _, second, _ = seed(tmp_path, 2)
    image = library.image(first, "original")
    result = library.delete_labels(deletion((first, 1), (second, 1)))
    assert result["deleted"] == 2 and result["scope"] == "labels"
    assert library.manifest() == b""
    assert library.list_samples()["categories"] == []
    assert library.list_samples(status="pending")["total"] == 2
    assert library.detail(first)["annotation"] is None
    assert library.detail(first)["revision"] == 2
    assert library.image(first, "original") == image
    assert (source / f"{first}.json").exists()
    history = next((library.root / "trash").glob(f"*/{first}/history/revision-00000001.json"))
    assert json.loads(history.read_text())["people"][0]["identity"] == "儿子"
    payload = {
        "expected_revision": 1,
        "status": "confirmed",
        "people": [
            {
                "id": "face-0",
                "identity": "妈妈",
                "posture": "lying",
                "action": "none",
            }
        ],
    }
    with pytest.raises(AnnotationConflict):
        library.save(first, AnnotationUpdate.model_validate(payload))
    saved = library.save(
        first, AnnotationUpdate.model_validate({**payload, "expected_revision": 2})
    )
    assert saved["revision"] == saved["annotation"]["revision"] == 3
    assert history.exists()  # Deletion does not overwrite or erase past human evidence.


def test_delete_when_bundle_is_only_remaining_source(tmp_path):
    library, sample_id, source = seed(tmp_path)
    (source / f"{sample_id}.json").unlink()
    (source / f"{sample_id}.jpg").unlink()
    library.delete_labels(deletion((sample_id, 1)))
    restarted = AnnotationLibrary("", tmp_path / "state")
    assert restarted.detail(sample_id)["annotation"] is None
    assert restarted.list_samples()["total"] == 1
    assert restarted.image(sample_id).startswith(b"\xff\xd8")


def test_delete_prevalidates_every_revision_and_pending_selection(tmp_path):
    library, first, _ = seed(tmp_path)
    _, second, _ = seed(tmp_path, 2)
    with pytest.raises(AnnotationConflict):
        library.delete_labels(deletion((first, 1), (second, 0)))
    assert library.detail(first)["annotation"]["revision"] == 1
    assert library.detail(second)["annotation"]["revision"] == 1
    assert not (library.root / "trash").exists()
    _, pending, _ = seed(tmp_path, 3, confirmed=False)
    with pytest.raises(ValueError, match="尚无人工"):
        library.delete_labels(deletion((pending, 0)))


def test_delete_rolls_every_item_back_on_write_failure(tmp_path, monkeypatch):
    library, first, _ = seed(tmp_path)
    _, second, _ = seed(tmp_path, 2)
    write = annotations._write
    interrupted = False

    def fail_once(path, content, **kwargs):
        nonlocal interrupted
        if path == library.samples / second / "tombstone.json" and not interrupted:
            interrupted = True
            raise OSError("simulated disk error")
        write(path, content, **kwargs)

    monkeypatch.setattr(annotations, "_write", fail_once)
    with pytest.raises(OSError):
        library.delete_labels(deletion((first, 1), (second, 1)))
    assert library.detail(first)["revision"] == library.detail(second)["revision"] == 1
    assert len(library.manifest().splitlines()) == 2
    assert all(library.detail(sample_id)["annotation"] for sample_id in [first, second])
    assert (
        json.loads(next((library.root / "trash").glob("*/transaction.json")).read_text())["status"]
        == "rolled_back"
    )


def test_startup_recovers_interrupted_deletion(tmp_path):
    library, sample_id, source = seed(tmp_path)
    bundle = library.samples / sample_id
    batch = library.root / "trash" / ("a" * 32)
    backup = batch / sample_id
    backup.mkdir(parents=True)
    journal = {
        "status": "pending",
        "scope": "labels",
        "items": [
            {
                "id": sample_id,
                "revision": 1,
                "previous_tombstone": None,
            }
        ],
    }
    (batch / "transaction.json").write_text(json.dumps(journal))
    (bundle / "annotation.json").rename(backup / "annotation.json")
    (bundle / "history").rename(backup / "history")
    (bundle / "tombstone.json").write_text('{"revision":2}')
    restarted = AnnotationLibrary(str(source), tmp_path / "state")
    assert restarted.detail(sample_id)["revision"] == 1
    assert restarted.detail(sample_id)["annotation"]
    assert json.loads((batch / "transaction.json").read_text())["status"] == "rolled_back"


def test_delete_refuses_symlinks_before_mutation(tmp_path):
    library, sample_id, _ = seed(tmp_path)
    (library.samples / sample_id / "history" / "unexpected").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="符号链接"):
        library.delete_labels(deletion((sample_id, 1)))
    assert library.detail(sample_id)["revision"] == 1
    assert not (library.root / "trash").exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"ids": ["../secret"]},
        {"ids": ["20261304-120001-123456"]},
        {"ids": ["20261004-120001-123456"] * 2},
        {"posture": "running"},
        {"label_source": "prediction"},
        {"identity": "a" * 65},
        {"ids": ["20261004-120001-123456"] * 201},
        {"extra": "secret"},
    ],
)
def test_batch_selection_schema(payload):
    with pytest.raises(ValidationError):
        BatchSelection.model_validate(payload)


def test_batch_delete_schema():
    with pytest.raises(ValidationError):
        BatchDelete(items=[])
    with pytest.raises(ValidationError):
        deletion(("20261004-120001-123456", True))
    with pytest.raises(ValidationError):
        deletion(("20261004-120001-123456", 1), ("20261004-120001-123456", 2))


def test_bulk_photo_removal_covers_pending_source_and_permanent_bundle(tmp_path):
    library, first, source = seed(tmp_path)
    _, pending, _ = seed(tmp_path, 2, confirmed=False)
    _, keep, _ = seed(tmp_path, 3)
    gallery = source / "人脸审核相册.html"
    gallery.write_text("keep gallery unchanged")
    original = library.image(first, "original")
    result = library.delete_labels(deletion((first, 1), (pending, 0), scope="photos"))
    assert result["deleted"] == 2 and result["scope"] == "photos"
    assert [item["id"] for item in library.list_samples()["items"]] == [keep]
    assert gallery.read_text() == "keep gallery unchanged"
    assert library.detail(keep)["revision"] == 1
    batch = next((library.root / "trash").iterdir())
    assert (batch / first / "bundle" / "model.json").exists()
    assert (batch / first / "source-original.jpg").read_bytes() == original
    assert (batch / pending / "source-model.json").exists()
    assert not (batch / pending / "bundle").exists()
    for sample_id in [first, pending]:
        assert not (library.samples / sample_id).exists()
        assert not (source / f"{sample_id}.jpg").exists()
        assert not (source / f"{sample_id}.json").exists()
        assert not (source / "originals" / f"{sample_id}.jpg").exists()
        with pytest.raises(FileNotFoundError):
            library.detail(sample_id)
        with pytest.raises(FileNotFoundError):
            library.image(sample_id, "original")
    assert [json.loads(line)["id"] for line in library.manifest().splitlines()] == [keep]
    for path in batch.rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)


def test_photo_removal_handles_bundle_only_and_legacy_no_original(tmp_path):
    library, sample_id, source = seed(tmp_path, raw=False)
    (source / f"{sample_id}.jpg").unlink()
    (source / f"{sample_id}.json").unlink()
    library.delete_labels(deletion((sample_id, 1), scope="photos"))
    assert library.list_samples()["total"] == 0 and library.manifest() == b""
    bundle = next((library.root / "trash").glob(f"*/{sample_id}/bundle"))
    assert (bundle / "annotated.jpg").exists() and not (bundle / "original.jpg").exists()
    assert AnnotationLibrary("", tmp_path / "state").list_samples()["total"] == 0


def test_photo_delete_prevalidates_stale_revision_and_all_source_json(tmp_path):
    library, first, source = seed(tmp_path)
    _, second, _ = seed(tmp_path, 2)
    with pytest.raises(AnnotationConflict):
        library.delete_labels(deletion((first, 1), (second, 0), scope="photos"))
    assert library.list_samples()["total"] == 2
    (source / f"{second}.json").write_text("invalid")
    with pytest.raises(ValueError):
        library.delete_labels(deletion((first, 1), (second, 1), scope="photos"))
    assert (source / f"{first}.jpg").exists()
    assert (library.samples / first).exists()
    assert not (library.root / "trash").exists()


def test_photo_delete_full_rollback_on_later_failure(tmp_path, monkeypatch):
    library, first, source = seed(tmp_path)
    _, second, _ = seed(tmp_path, 2, confirmed=False)
    write = annotations._write
    interrupted = False

    def fail_once(path, content, **kwargs):
        nonlocal interrupted
        if path == library.root / "deleted" / f"{second}.json" and not interrupted:
            interrupted = True
            raise OSError("simulated marker write failure")
        write(path, content, **kwargs)

    monkeypatch.setattr(annotations, "_write", fail_once)
    with pytest.raises(OSError):
        library.delete_labels(deletion((first, 1), (second, 0), scope="photos"))
    assert library.detail(first)["annotation"]["revision"] == 1
    assert library.detail(second)["annotation"] is None
    assert library.detail(second)["revision"] == 0
    assert library.list_samples()["total"] == 2
    assert len(library.manifest().splitlines()) == 1
    for sample_id in [first, second]:
        assert (source / f"{sample_id}.jpg").exists()
        assert (source / f"{sample_id}.json").exists()
        assert (source / "originals" / f"{sample_id}.jpg").exists()
    assert not list((library.root / "deleted").iterdir())


def test_photo_delete_restart_recovers_interrupted_move(tmp_path):
    library, sample_id, source = seed(tmp_path)
    body = deletion((sample_id, 1), scope="photos")
    entries = photo_batches._preflight_photos(library, body)
    batch = library.root / "trash" / ("b" * 32)
    backup = batch / sample_id
    backup.mkdir(parents=True)
    journal = {"scope": "photos", "status": "pending", "sample_dir": str(source), "items": entries}
    (batch / "transaction.json").write_text(json.dumps(journal))
    (library.samples / sample_id).rename(backup / "bundle")
    (source / f"{sample_id}.jpg").rename(backup / "source-annotated.jpg")
    restarted = AnnotationLibrary(str(source), tmp_path / "state")
    assert restarted.detail(sample_id)["revision"] == 1
    assert restarted.image(sample_id).startswith(b"\xff\xd8")
    assert (source / f"{sample_id}.jpg").exists()
    assert json.loads((batch / "transaction.json").read_text())["status"] == "rolled_back"


def test_deleted_stamp_cannot_reappear_from_stale_source(tmp_path):
    library, sample_id, source = seed(tmp_path, confirmed=False)
    library.delete_labels(deletion((sample_id, 0), scope="photos"))
    # A stale copy with the same identifier must not bypass the deletion revision.
    backup = next((library.root / "trash").glob(f"*/{sample_id}"))
    (source / f"{sample_id}.jpg").write_bytes((backup / "source-annotated.jpg").read_bytes())
    (source / f"{sample_id}.json").write_bytes((backup / "source-model.json").read_bytes())
    assert library.list_samples()["total"] == 0
    with pytest.raises(FileNotFoundError):
        library.detail(sample_id)
    with pytest.raises(FileNotFoundError):
        library.save(sample_id, AnnotationUpdate(expected_revision=0, status="excluded"))


def test_photo_delete_rejects_source_symlink_and_selected_bundle_symlink(tmp_path):
    library, first, source = seed(tmp_path)
    raw = source / "originals" / f"{first}.jpg"
    raw.unlink()
    raw.symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="符号链接"):
        library.delete_labels(deletion((first, 1), scope="photos"))
    assert (library.samples / first).exists() and not (library.root / "trash").exists()
    raw.unlink()
    (library.samples / first / "private-link").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="符号链接"):
        library.delete_labels(deletion((first, 1), scope="photos"))
    assert (source / f"{first}.jpg").exists()


def test_photo_delete_detects_external_file_changes_and_rolls_back(tmp_path, monkeypatch):
    library, sample_id, source = seed(tmp_path)
    preflight = photo_batches._preflight_photos

    def changed(library_arg, body):
        entries = preflight(library_arg, body)
        (source / f"{sample_id}.json").write_text('{"changed":true}')
        return entries

    monkeypatch.setattr(photo_batches, "_preflight_photos", changed)
    with pytest.raises(AnnotationConflict, match="文件已变化"):
        library.delete_labels(deletion((sample_id, 1), scope="photos"))
    assert library.detail(sample_id)["revision"] == 1
    assert (library.samples / sample_id).exists()
    assert (source / f"{sample_id}.jpg").exists()


@pytest.mark.parametrize("scope", ["labels", "photos"])
def test_sustained_storage_fault_blocks_reads_and_saves_until_recovery(
    tmp_path, monkeypatch, scope
):
    library, sample_id, source = seed(tmp_path)
    write = annotations._write
    rename = Path.rename
    marker = (
        library.samples / sample_id / "tombstone.json"
        if scope == "labels"
        else library.root / "deleted" / f"{sample_id}.json"
    )

    def failed_write(path, content, **kwargs):
        if path == marker:
            raise OSError("persistent marker storage fault")
        write(path, content, **kwargs)

    def failed_recovery_rename(path, target):
        if library.root / "trash" in path.parents:
            raise OSError("persistent rollback storage fault")
        return rename(path, target)

    payload = AnnotationUpdate.model_validate(
        {
            "expected_revision": 0,
            "status": "confirmed",
            "people": [
                {
                    "id": "face-0",
                    "identity": "妈妈",
                    "posture": "standing",
                    "action": "none",
                }
            ],
        }
    )
    with monkeypatch.context() as failure:
        failure.setattr(annotations, "_write", failed_write)
        failure.setattr(Path, "rename", failed_recovery_rename)
        with pytest.raises(OSError):
            library.delete_labels(deletion((sample_id, 1), scope=scope))
        # No same-process operation may mistake half-moved labels for revision zero.
        for operation in [
            lambda: library.detail(sample_id),
            lambda: library.list_samples(),
            lambda: library.image(sample_id),
            lambda: library.save(sample_id, payload),
            lambda: library.manifest(),
            lambda: library.export_zip(BatchSelection()),
        ]:
            with pytest.raises(OSError, match="storage fault"):
                operation()
    assert library.detail(sample_id)["revision"] == 1
    assert library.detail(sample_id)["annotation"]["people"][0]["identity"] == "儿子"
    with pytest.raises(AnnotationConflict):
        library.save(sample_id, payload)
    restarted = AnnotationLibrary(str(source), tmp_path / "state")
    assert restarted.detail(sample_id)["revision"] == 1
    assert (
        json.loads(next((library.root / "trash").glob("*/transaction.json")).read_text())["status"]
        == "rolled_back"
    )
