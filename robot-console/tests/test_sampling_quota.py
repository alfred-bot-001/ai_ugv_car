"""Singapore day limits survive restarts, deletion, races and interrupted commits."""

import asyncio
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from tvpet_console.annotations import AnnotationLibrary
from tvpet_console.photo_batches import BatchDelete
from tvpet_console.pose_review import ReviewRecorder
from tvpet_console.sampling_quota import SINGAPORE, DailyLimitReached, SamplingQuota


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        moment = datetime(2026, 10, 4, 12, tzinfo=SINGAPORE)

        @classmethod
        def now(cls, tz=None):
            return cls.moment.astimezone(tz) if tz else cls.moment

    monkeypatch.setattr("tvpet_console.pose_review.datetime", Clock)
    return Clock


@pytest.fixture
def jpeg():
    output = io.BytesIO()
    Image.new("RGB", (640, 480), "gray").save(output, "JPEG")
    return output.getvalue()


def metadata():
    return {
        "sequence": 1,
        "recognition": "active",
        "faces": [{"bbox": [20, 20, 100, 100], "label": "father"}],
        "pose": {"state": "active", "people": []},
    }


def recorder(tmp_path):
    return ReviewRecorder(str(tmp_path / "photos"), state_dir=tmp_path / "state")


def committed(root, sample_id, moment, jpeg, *, original=False):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{sample_id}.jpg").write_bytes(jpeg)
    (root / f"{sample_id}.json").write_text(
        json.dumps(
            {
                **metadata(),
                "sampled_at": moment.isoformat(),
                "original_available": original,
            }
        )
    )
    if original:
        (root / "originals").mkdir(exist_ok=True)
        (root / "originals" / f"{sample_id}.jpg").write_bytes(jpeg)


def test_fiftieth_save_stops_fifty_first_and_no_person_cannot_clear_limit(tmp_path, clock, jpeg):
    review = recorder(tmp_path)
    for _ in range(50):
        review.save(jpeg, metadata())
        clock.moment += timedelta(seconds=31)
    assert review.status()["daily_saved"] == 50
    assert review.status()["daily_limit"] == 50
    assert review.status()["state"] == "daily_limit"
    with pytest.raises(DailyLimitReached):
        review.save(jpeg, metadata())
    review.submit(jpeg, {"pose": {"state": "active", "people": []}, "faces": []})
    assert review.status()["state"] == "daily_limit"
    assert len(list((tmp_path / "photos").glob("*.json"))) == 50
    assert asyncio.run(review.can_capture()) is False


def test_restart_and_user_deletion_never_refund_daily_slots(tmp_path, clock, jpeg):
    review = recorder(tmp_path)
    review.save(jpeg, metadata())
    clock.moment += timedelta(seconds=31)
    review.save(jpeg, metadata())
    root = tmp_path / "photos"
    for path in root.rglob("*"):
        if path.is_file():
            path.unlink()
    restarted = recorder(tmp_path)
    assert asyncio.run(restarted.can_capture()) is True
    assert restarted.status()["daily_saved"] == 2
    assert restarted.next_at > 0
    assert (tmp_path / "state" / "sampling-quota" / "quota.sqlite3").is_file()


def test_midnight_uses_singapore_not_utc_and_resumes_without_person_frame(tmp_path, clock, jpeg):
    clock.moment = datetime(2026, 10, 4, 15, 59, 59, tzinfo=UTC)
    review = ReviewRecorder(str(tmp_path / "photos"), state_dir=tmp_path / "state", daily_limit=1)
    review.save(jpeg, metadata())
    assert review.status()["day"] == "2026-10-04"
    assert not asyncio.run(review.can_capture())
    clock.moment += timedelta(seconds=1)
    review.submit(jpeg, {"pose": {"state": "active", "people": []}, "faces": []})
    assert review.status()["day"] == "2026-10-05"
    assert review.status()["daily_saved"] == 0
    assert review.status()["state"] == "waiting_person"
    assert asyncio.run(review.can_capture())
    clock.moment += timedelta(seconds=31)
    review.save(jpeg, metadata())
    assert review.status()["daily_saved"] == 1


def test_initial_migration_converts_sampled_at_and_retains_more_than_limit(tmp_path, clock, jpeg):
    root = tmp_path / "photos"
    for index in range(53):
        # UTC October 3 is Singapore October 4.
        committed(root, f"20261003-160000-{index:06d}", datetime(2026, 10, 3, 16, tzinfo=UTC), jpeg)
    committed(root, "20261003-155959-999999", datetime(2026, 10, 3, 15, 59, 59, tzinfo=UTC), jpeg)
    review = recorder(tmp_path)
    assert not asyncio.run(review.can_capture())
    assert review.status()["daily_saved"] == 53
    assert review.status()["state"] == "daily_limit"
    assert len(list(root.glob("*.json"))) == 54


def test_migration_counts_recycled_samples_once_and_ignores_duplicate_backups(
    tmp_path, clock, jpeg
):
    root = tmp_path / "photos"
    sample_id = "20261004-120000-123456"
    committed(root, sample_id, clock.moment, jpeg)
    library = AnnotationLibrary(str(root), tmp_path / "state")
    library.delete_labels(
        BatchDelete.model_validate(
            {
                "scope": "photos",
                "items": [{"id": sample_id, "expected_revision": 0}],
            }
        )
    )
    # Copy the archived raw metadata/image to a permanent sample backup as well.
    recycle = next((tmp_path / "state" / "photo-labels" / "trash").glob(f"*/{sample_id}"))
    bundle = tmp_path / "state" / "photo-labels" / "samples" / sample_id
    bundle.mkdir(parents=True)
    (bundle / "model.json").write_bytes((recycle / "source-model.json").read_bytes())
    (bundle / "annotated.jpg").write_bytes((recycle / "source-annotated.jpg").read_bytes())
    review = recorder(tmp_path)
    assert asyncio.run(review.can_capture())
    assert review.status()["daily_saved"] == 1


def test_two_recorders_cannot_overshoot_one_remaining_slot(tmp_path, clock, jpeg):
    root = tmp_path / "photos"
    for index in range(49):
        committed(root, f"20261004-110000-{index:06d}", clock.moment, jpeg)
    first, second = recorder(tmp_path), recorder(tmp_path)

    def save(review):
        try:
            review.save(jpeg, metadata())
            return "saved"
        except DailyLimitReached:
            return "limited"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(save, [first, second])) == ["limited", "saved"]
    assert len(list(root.glob("*.json"))) == 50
    assert not asyncio.run(first.can_capture())
    assert not asyncio.run(second.can_capture())


def test_same_stamp_collision_never_double_counts(tmp_path, clock, jpeg):
    review = recorder(tmp_path)
    review.save(jpeg, metadata())
    with pytest.raises(FileExistsError):
        review.save(jpeg, metadata())
    assert asyncio.run(review.can_capture())
    assert review.status()["daily_saved"] == 1


def test_failed_photo_write_does_not_consume_slot_and_recovery_allows_retry(
    tmp_path, clock, jpeg, monkeypatch
):
    review = recorder(tmp_path)
    write = ReviewRecorder._write_sample
    with monkeypatch.context() as failure:
        failure.setattr(
            ReviewRecorder,
            "_write_sample",
            staticmethod(lambda contents: (_ for _ in ()).throw(OSError("write failed"))),
        )
        with pytest.raises(OSError):
            review.save(jpeg, metadata())
    assert asyncio.run(review.can_capture())
    assert review.status()["daily_saved"] == 0
    assert ReviewRecorder._write_sample is write
    review.save(jpeg, metadata())
    assert review.status()["daily_saved"] == 1


@pytest.mark.parametrize("delete_before_recovery", [False, True])
def test_photo_committed_quota_write_failed_is_counted_after_recovery(
    tmp_path, clock, jpeg, monkeypatch, delete_before_recovery
):
    review = recorder(tmp_path)
    with monkeypatch.context() as failure:
        failure.setattr(
            SamplingQuota,
            "_complete",
            lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")),
        )
        with pytest.raises(OSError):
            review.save(jpeg, metadata())
    root = tmp_path / "photos"
    sample_id = next(root.glob("*.json")).stem
    if delete_before_recovery:
        library = AnnotationLibrary(str(root), tmp_path / "state")
        library.delete_labels(
            BatchDelete.model_validate(
                {
                    "scope": "photos",
                    "items": [{"id": sample_id, "expected_revision": 0}],
                }
            )
        )
    restarted = recorder(tmp_path)
    assert asyncio.run(restarted.can_capture())
    assert restarted.status()["daily_saved"] == 1


def test_unknown_pending_blocks_current_day_and_next_day_can_resume(tmp_path, clock, jpeg):
    review = recorder(tmp_path)
    assert asyncio.run(review.can_capture())
    with sqlite3.connect(review.quota.path) as connection:
        connection.execute(
            "INSERT INTO pending VALUES(1,?,?,?)",
            ("20261004-120000-123456", "2026-10-04", clock.moment.isoformat()),
        )
    assert not asyncio.run(review.can_capture())
    assert review.status()["state"] == "paused"
    assert review.status()["daily_saved"] == 0
    with pytest.raises(OSError, match="提交状态"):
        review.save(jpeg, metadata())
    clock.moment += timedelta(days=1)
    assert asyncio.run(review.can_capture())
    assert review.status()["day"] == "2026-10-05"
    with sqlite3.connect(review.quota.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM unresolved").fetchone()[0] == 1


def test_incomplete_pending_json_neither_counts_success_nor_frees_reservation(
    tmp_path, clock, jpeg
):
    review = recorder(tmp_path)
    assert asyncio.run(review.can_capture())
    sample_id = "20261004-120000-123456"
    root = tmp_path / "photos"
    committed(root, sample_id, clock.moment, jpeg, original=True)
    (root / "originals" / f"{sample_id}.jpg").unlink()
    with sqlite3.connect(review.quota.path) as connection:
        connection.execute(
            "INSERT INTO pending VALUES(1,?,?,?)",
            (sample_id, "2026-10-04", clock.moment.isoformat()),
        )
    assert not asyncio.run(review.can_capture())
    assert review.status()["daily_saved"] == 0 and review.status()["state"] == "paused"
    with sqlite3.connect(review.quota.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 1
    clock.moment += timedelta(days=1)
    assert asyncio.run(review.can_capture())
    assert review.status()["daily_saved"] == 0


@pytest.mark.parametrize(
    "failure_kind", ["root_file", "root_symlink", "lock_symlink", "database_corrupt"]
)
def test_initial_storage_failure_is_closed_with_unknown_count(tmp_path, clock, failure_kind):
    review = recorder(tmp_path)
    root = tmp_path / "state" / "sampling-quota"
    root.parent.mkdir()
    if failure_kind == "root_file":
        root.write_bytes(b"not a directory")
    elif failure_kind == "root_symlink":
        root.symlink_to(tmp_path / "outside", target_is_directory=True)
    else:
        root.mkdir()
        if failure_kind == "lock_symlink":
            (root / ".lock").symlink_to(tmp_path / "outside")
        else:
            (root / "quota.sqlite3").write_bytes(b"not sqlite")
    assert not asyncio.run(review.can_capture())
    assert review.status()["state"] == "paused"
    assert review.status()["error"]
    assert review.status()["daily_saved"] is None


@pytest.mark.parametrize("corruption", ["drop_days", "negative_count", "nan_time"])
def test_initialized_ledger_corruption_never_resets_count(tmp_path, clock, corruption):
    review = recorder(tmp_path)
    assert asyncio.run(review.can_capture())
    with sqlite3.connect(review.quota.path) as connection:
        if corruption == "drop_days":
            connection.execute("DROP TABLE days")
        elif corruption == "negative_count":
            connection.execute("INSERT INTO days VALUES('2026-10-04',-1)")
        else:
            connection.execute("INSERT INTO meta VALUES('last_saved_epoch','NaN')")
    assert not asyncio.run(review.can_capture())
    assert review.status()["state"] == "paused"


def test_corrupt_or_naive_historical_time_fails_migration_closed(tmp_path, clock, jpeg):
    root = tmp_path / "photos"
    committed(root, "20261004-120000-123456", clock.moment.replace(tzinfo=None), jpeg)
    review = recorder(tmp_path)
    assert not asyncio.run(review.can_capture())
    assert review.status()["daily_saved"] is None
    assert review.status()["error"]


def test_status_is_cached_without_disk_lock(tmp_path, clock, monkeypatch):
    review = recorder(tmp_path)
    assert asyncio.run(review.can_capture())
    monkeypatch.setattr(
        SamplingQuota,
        "_locked",
        lambda self: (_ for _ in ()).throw(AssertionError("disk lock in status")),
    )
    assert review.status()["daily_saved"] == 0
    assert review.status()["timezone"] == "Asia/Singapore"


def test_private_permissions(tmp_path, clock, jpeg):
    review = recorder(tmp_path)
    review.save(jpeg, metadata())
    root = tmp_path / "state" / "sampling-quota"
    assert root.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in root.iterdir() if path.is_file())
