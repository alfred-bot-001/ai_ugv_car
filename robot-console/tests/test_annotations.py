"""Human review stays separate from model evidence and survives temporary cleanup."""

import hashlib
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image
from pydantic import ValidationError
from tvpet_console import annotations
from tvpet_console.annotations import AnnotationConflict, AnnotationLibrary, AnnotationUpdate

STAMP = "20261003-093029-935725"
SECOND = "20261003-100405-865974"


def jpeg(color="gray"):
    stream = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(stream, "JPEG")
    return stream.getvalue()


def model():
    points = [[0.5, 0.5, 0, 0.99] for _ in range(33)]
    points[0][:2] = [0.5, 0.2]
    return {
        "sequence": 12,
        "sampled_at": "2026-10-03T09:30:29+08:00",
        "timestamp_ns": 99,
        "pose": {
            "model": "mediapipe-pose-lite",
            "rules": "geometry-v1",
            "people": [
                {
                    "track_id": 7,
                    "identity": "outdated-label",
                    "posture": "standing",
                    "action": "none",
                    "score": 0.8,
                    "keypoints": points,
                }
            ],
        },
        "faces": [{"bbox": [290, 60, 350, 120], "label": "爸爸", "similarity": 0.51}],
    }


def sample(tmp_path, *, raw=False, data=None, stamp=STAMP):
    root = tmp_path / "review"
    root.mkdir(exist_ok=True)
    (root / f"{stamp}.jpg").write_bytes(jpeg("green"))
    (root / f"{stamp}.json").write_text(json.dumps(data or model(), ensure_ascii=False))
    if raw:
        (root / "originals").mkdir(exist_ok=True)
        (root / "originals" / f"{stamp}.jpg").write_bytes(jpeg("red"))
    return root


def update(revision=0, *, status="confirmed", identity="儿子", people=None):
    return AnnotationUpdate.model_validate(
        {
            "expected_revision": revision,
            "status": status,
            "people": people
            if people is not None
            else [
                {
                    "id": "pose-0",
                    "identity": identity,
                    "posture": "sitting",
                    "action": "greeting",
                }
            ],
            "notes": "人工确认",
        }
    )


def test_evidence_is_permanent_exact_and_separate(tmp_path):
    root = sample(tmp_path, raw=True)
    library = AnnotationLibrary(str(root), tmp_path / "state")
    before = (root / f"{STAMP}.json").read_bytes()
    raw = (root / "originals" / f"{STAMP}.jpg").read_bytes()
    detail = library.detail(STAMP)
    assert detail["annotation"] is None and detail["original_available"] is True
    assert detail["predictions"][0]["identity"] == "爸爸"
    assert len(detail["predictions"]) == 1 and detail["predictions"][0]["bbox"]
    result = library.save(STAMP, update())
    assert result["annotation"]["revision"] == 1
    assert result["annotation"]["people"][0]["identity"] == "儿子"
    assert result["predictions"] == detail["predictions"]
    bundle = library.samples / STAMP
    assert (bundle / "model.json").read_bytes() == before
    assert library.image(STAMP, "original") == raw
    assert library.image(STAMP) != raw
    provenance = json.loads((bundle / "provenance.json").read_text())
    assert provenance["sha256"]["original.jpg"] == hashlib.sha256(raw).hexdigest()
    record = json.loads(library.manifest())
    assert record["annotation"]["people"][0]["keypoints"] == detail["predictions"][0]["keypoints"]
    assert record["images"]["original"] == f"samples/{STAMP}/original.jpg"
    assert "image_url" not in str(record) and str(tmp_path) not in str(record)
    assert (root / f"{STAMP}.json").read_bytes() == before
    for path in library.root.rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    assert library.root.stat().st_mode & 0o777 == 0o700
    # Human labels and copied evidence remain available when temporary files disappear.
    (root / f"{STAMP}.jpg").unlink()
    (root / f"{STAMP}.json").unlink()
    (root / "originals" / f"{STAMP}.jpg").unlink()
    restarted = AnnotationLibrary("", tmp_path / "state")
    assert restarted.list_samples()["total"] == 1
    assert restarted.image(STAMP, "original") == raw


def test_legacy_annotated_image_never_claimed_as_original(tmp_path):
    root = sample(tmp_path)
    library = AnnotationLibrary(str(root), tmp_path / "state")
    assert not library.detail(STAMP)["original_available"]
    assert library.detail(STAMP)["original_url"] is None
    with pytest.raises(FileNotFoundError):
        library.image(STAMP, "original")
    library.save(STAMP, update())
    assert not (library.samples / STAMP / "original.jpg").exists()
    exported = json.loads(library.manifest())
    assert not exported["original_available"] and exported["images"]["original"] is None


def test_revision_conflict_exclude_and_reconfirm(tmp_path):
    root = sample(tmp_path)
    library = AnnotationLibrary(str(root), tmp_path / "state")
    library.save(STAMP, update())
    history = library.samples / STAMP / "history" / "revision-00000001.json"
    original_revision = history.read_bytes()
    with pytest.raises(AnnotationConflict):
        library.save(STAMP, update(identity="妈妈"))
    library.save(STAMP, update(1, status="excluded", people=[]))
    assert library.manifest() == b""
    assert library.list_samples(status="excluded")["total"] == 1
    library.save(STAMP, update(2, identity="妈妈"))
    assert history.read_bytes() == original_revision
    assert len(list(history.parent.glob("*.json"))) == 3
    assert json.loads(library.manifest())["annotation"]["revision"] == 3


def test_concurrent_editors_cannot_overwrite(tmp_path):
    root = sample(tmp_path)
    first = AnnotationLibrary(str(root), tmp_path / "state")
    second = AnnotationLibrary(str(root), tmp_path / "state")

    def save(library):
        try:
            library.save(STAMP, update())
            return "saved"
        except AnnotationConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(save, [first, second])) == ["conflict", "saved"]
    assert first.detail(STAMP)["annotation"]["revision"] == 1


def test_manual_missed_person_and_reject_foreign_prediction(tmp_path):
    root = sample(tmp_path)
    library = AnnotationLibrary(str(root), tmp_path / "state")
    with pytest.raises(ValueError, match="不属于"):
        library.save(
            STAMP,
            update(
                people=[
                    {
                        "id": "pose-5",
                        "identity": "爸爸",
                        "posture": "lying",
                        "action": "none",
                    }
                ]
            ),
        )
    with pytest.raises(ValueError, match="至少"):
        library.save(STAMP, update(people=[]))
    library.save(
        STAMP,
        update(
            people=[
                {
                    "id": "manual-new",
                    "identity": "妈妈",
                    "posture": "other",
                    "action": "other",
                }
            ]
        ),
    )
    person = json.loads(library.manifest())["annotation"]["people"][0]
    assert person["id"] == "manual-new" and person["keypoints"] == [] and person["bbox"] is None


def test_multiple_bodies_never_share_identity_and_keep_unmatched_faces(tmp_path):
    data = model()
    data["pose"]["people"] *= 2
    data["faces"].append({"bbox": [0, 0, 40, 40], "label": "妈妈"})
    root = sample(tmp_path, data=data)
    library = AnnotationLibrary(str(root), tmp_path / "state")
    people = library.detail(STAMP)["predictions"]
    assert [person["id"] for person in people] == ["pose-0", "pose-1", "face-0", "face-1"]
    assert people[0]["identity"] == people[1]["identity"] == "人物未确认"
    assert people[0]["bbox"] is None and people[2]["identity"] == "爸爸"
    data["pose"]["people"] = []
    (root / f"{STAMP}.json").write_text(json.dumps(data))
    assert [person["id"] for person in library.detail(STAMP)["predictions"]] == ["face-0", "face-1"]


def test_dynamic_list_search_pagination_and_stats(tmp_path):
    root = sample(tmp_path)
    library = AnnotationLibrary(str(root), tmp_path / "state")
    assert library.list_samples()["stats"]["pending"] == 1
    sample(tmp_path, stamp=SECOND, raw=True)
    page = library.list_samples(limit=1)
    assert page["items"][0]["id"] == SECOND and page["pages"] == 2
    assert page["items"][0]["predictions"][0]["keypoints"] == []
    assert len(library.detail(SECOND)["predictions"][0]["keypoints"]) == 33
    assert library.list_samples(page=2, limit=1)["items"][0]["id"] == STAMP
    library.save(STAMP, update())
    assert library.list_samples(q="儿子")["total"] == 1
    assert library.list_samples(q="坐着")["total"] == 1
    assert library.list_samples(status="confirmed")["stats"] == {
        "total": 2,
        "pending": 1,
        "confirmed": 1,
        "excluded": 0,
        "originals": 1,
    }
    assert library.list_samples(page=10)["items"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"limit": 101},
        {"status": "broken"},
        {"q": "x" * 101},
    ],
)
def test_bad_query(tmp_path, params):
    library = AnnotationLibrary("", tmp_path / "state")
    with pytest.raises(ValueError):
        library.list_samples(**params)
    assert library.list_samples()["items"] == []


@pytest.mark.parametrize(
    "sample_id", ["../secret", "/etc/passwd", "20261303-093029-935725", STAMP + "\n"]
)
def test_path_traversal_rejected(tmp_path, sample_id):
    library = AnnotationLibrary("", tmp_path / "state")
    with pytest.raises(ValueError):
        library.detail(sample_id)
    with pytest.raises(ValueError):
        library.image(sample_id)


@pytest.mark.parametrize("target", ["json", "jpg", "originals"])
def test_symlink_source_is_rejected(tmp_path, target):
    root = sample(tmp_path)
    library = AnnotationLibrary(str(root), tmp_path / "state")
    secret = tmp_path / "secret"
    secret.write_bytes(b"private-secret")
    if target == "originals":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / f"{STAMP}.jpg").write_bytes(jpeg())
        (root / "originals").symlink_to(outside, target_is_directory=True)
    else:
        path = root / f"{STAMP}.{target}"
        path.unlink()
        path.symlink_to(secret)
    with pytest.raises(ValueError):
        library.detail(STAMP)
    assert library.list_samples()["total"] == 0
    with pytest.raises(ValueError):
        library.save(STAMP, update())


def test_symlink_root_or_archive_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        AnnotationLibrary("", link / "state")
    library = AnnotationLibrary(str(link), tmp_path / "state")
    with pytest.raises(ValueError):
        library.list_samples()
    (library.samples / STAMP).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        library.detail(STAMP)


def test_bad_metadata_or_unbounded_files_do_not_break_list(tmp_path, monkeypatch):
    root = sample(tmp_path)
    library = AnnotationLibrary(str(root), tmp_path / "state")
    sample(tmp_path, stamp=SECOND)
    (root / f"{SECOND}.json").write_text("not-json")
    assert library.list_samples()["total"] == 1
    (root / f"{SECOND}.json").write_text("[]")
    assert library.list_samples()["total"] == 1
    monkeypatch.setattr(annotations, "JSON_LIMIT", 4)
    assert library.list_samples()["total"] == 0
    with pytest.raises(ValueError):
        library.detail(STAMP)


def test_fifo_metadata_rejected_without_blocking(tmp_path):
    root = sample(tmp_path)
    library = AnnotationLibrary(str(root), tmp_path / "state")
    metadata = root / f"{STAMP}.json"
    metadata.unlink()
    os.mkfifo(metadata)
    assert library.list_samples()["total"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"expected_revision": -1},
        {"expected_revision": True},
        {"status": "pending"},
        {"notes": "n" * 1001},
        {"secret": "x"},
    ],
)
def test_schema_rejects_invalid_updates(change):
    data = update().model_dump()
    with pytest.raises(ValidationError):
        AnnotationUpdate.model_validate({**data, **change})


def test_schema_rejects_duplicate_people_and_unsafe_ids():
    data = update().model_dump()
    with pytest.raises(ValidationError):
        AnnotationUpdate.model_validate({**data, "people": data["people"] * 2})
    for bad in ["../x", "manual-../x", "pose-0\n"]:
        with pytest.raises(ValidationError):
            AnnotationUpdate.model_validate({**data, "people": [{**data["people"][0], "id": bad}]})
    for bad in ["", "x" * 65, "x\x00y"]:
        with pytest.raises(ValidationError):
            AnnotationUpdate.model_validate(
                {**data, "people": [{**data["people"][0], "identity": bad}]}
            )


def test_recovery_after_latest_write_interruption(tmp_path, monkeypatch):
    root = sample(tmp_path)
    library = AnnotationLibrary(str(root), tmp_path / "state")
    write = annotations._write

    def interrupted(path, content, **kwargs):
        if path.name == "annotation.json":
            raise OSError("simulated interruption")
        write(path, content, **kwargs)

    monkeypatch.setattr(annotations, "_write", interrupted)
    with pytest.raises(OSError):
        library.save(STAMP, update())
    monkeypatch.setattr(annotations, "_write", write)
    restarted = AnnotationLibrary("", tmp_path / "state")
    assert restarted.detail(STAMP)["annotation"]["revision"] == 1
    assert json.loads(restarted.manifest())["annotation"]["revision"] == 1
    restarted.save(STAMP, update(1))
    assert restarted.detail(STAMP)["annotation"]["revision"] == 2


def test_export_has_bound_and_skips_corrupt_history(tmp_path, monkeypatch):
    root = sample(tmp_path)
    library = AnnotationLibrary(str(root), tmp_path / "state")
    library.save(STAMP, update())
    monkeypatch.setattr(annotations, "MANIFEST_LIMIT", 10)
    with pytest.raises(OSError, match="32 MiB"):
        library.manifest()
    assert library.detail(STAMP)["annotation"]["revision"] == 1
    path = library.samples / STAMP / "history" / "revision-00000001.json"
    path.write_text('{"status":"invalid"}')
    assert library.list_samples()["total"] == 0
    assert library.manifest() == b""


def test_malformed_prediction_data_and_image_kind(tmp_path):
    data = model()
    data["pose"]["people"][0].update({"posture": {}, "action": [], "keypoints": []})
    data["faces"][0]["bbox"] = [float("nan"), 1, 2, 3]
    root = sample(tmp_path, data=data)
    library = AnnotationLibrary(str(root), tmp_path / "state")
    people = library.detail(STAMP)["predictions"]
    assert people[0]["posture"] == "unknown" and people[0]["keypoints"] == []
    with pytest.raises(ValueError):
        library.image(STAMP, "../../secrets")
    (root / f"{STAMP}.jpg").write_bytes(b"not-jpeg")
    with pytest.raises(ValueError, match="JPEG"):
        library.image(STAMP)
    with pytest.raises(ValueError, match="JPEG"):
        library.save(STAMP, update())
