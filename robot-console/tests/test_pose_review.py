"""Review samples stay local and preserve exact frame metadata and ambiguous identity."""

import asyncio
import io
import json
import os
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from tvpet_console.gateway import Gateway
from tvpet_console.pose_review import ReviewRecorder, associate
from tvpet_pi.protocol import MAX_JPEG


def freeze_time(monkeypatch):
    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 4, 12, 1, 2, 123456, tzinfo=UTC)

    monkeypatch.setattr("tvpet_console.pose_review.datetime", FixedTime)
    return FixedTime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")


def fixture():
    points = [[0.5, 0.5, 0, 0.99] for _ in range(33)]
    points[0][:2] = [0.5, 0.2]
    person = {
        "track_id": 1,
        "keypoints": points,
        "posture": "unknown",
        "posture_label": "无法判断",
        "action": "none",
        "action_label": "无明显动作",
        "score": 0,
    }
    faces = [{"bbox": [290, 60, 350, 120], "label": "爸爸", "known": True}]
    pose = associate({"state": "active", "people": [person]}, faces)
    return {
        "sequence": 123,
        "timestamp_ns": 9876,
        "recognition": "active",
        "pose": pose,
        "faces": faces,
    }


def jpeg():
    output = io.BytesIO()
    Image.new("RGB", (640, 480), "gray").save(output, "JPEG")
    return output.getvalue()


def test_identity_only_unique_same_frame():
    record = fixture()
    assert record["pose"]["people"][0]["identity"] == "爸爸"
    assert associate(record["pose"], [])["people"][0]["identity"] == "人物未确认"
    assert associate(record["pose"], record["faces"] * 2)["people"][0]["identity"] == "人物未确认"
    record["pose"]["people"] *= 2
    assert all(
        p["identity"] == "人物未确认" for p in associate(record["pose"], record["faces"])["people"]
    )


def test_saved_annotated_jpeg_json_permissions_and_storage_cap(tmp_path):
    recorder = ReviewRecorder(str(tmp_path))
    source = jpeg()
    stamp = recorder.save(source, fixture())
    picture = next(tmp_path.glob("*.jpg"))
    data = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert data["sampled_at"] == stamp and data["sequence"] == 123 and data["timestamp_ns"] == 9876
    assert data["pose"]["people"][0]["identity"] == "爸爸"
    assert data["original_available"] is True
    original = tmp_path / data["original_image"]
    assert original.read_bytes() == source
    assert original.name == picture.name
    assert original.stat().st_mode & 0o777 == 0o600
    assert original.parent.stat().st_mode & 0o777 == 0o700
    assert picture.stat().st_mode & 0o777 == 0o600
    assert next(tmp_path.glob("*.json")).stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    with Image.open(picture) as image:
        assert image.size == (640, 597)
        assert image.getpixel((320, 240)) != (128, 128, 128)
    recorder.max_bytes = 1
    with pytest.raises(OSError, match="上限"):
        recorder.save(jpeg(), fixture())
    assert len(list(tmp_path.glob("*.jpg"))) == 1
    restarted = ReviewRecorder(str(tmp_path))
    assert restarted.next_at - time.monotonic() > 28


@pytest.mark.parametrize("failure_step", [1, 2, 3])
def test_failed_commit_rolls_back_all_new_files_preserves_old_sample(
    tmp_path, monkeypatch, failure_step
):
    recorder = ReviewRecorder(str(tmp_path))
    recorder.save(jpeg(), fixture())
    existing = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    real_replace = os.replace
    calls = []

    def fail_commit(source, destination, **kwargs):
        calls.append(destination)
        if len(calls) == failure_step:
            raise OSError("simulated commit failure")
        real_replace(source, destination, **kwargs)

    monkeypatch.setattr("tvpet_console.pose_review.os.replace", fail_commit)
    with pytest.raises(OSError, match="commit failure"):
        recorder.save(jpeg(), fixture())
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == existing
    assert not list(tmp_path.rglob("*.tmp"))
    if failure_step == 3:
        assert calls[-1].endswith(".json")


@pytest.mark.parametrize("failure_step", [1, 2, 3])
def test_failed_temp_write_cleans_all_temporaries(tmp_path, monkeypatch, failure_step):
    calls = 0

    def fail_sync(fd):
        nonlocal calls
        calls += 1
        if calls == failure_step:
            raise OSError("simulated flush failure")

    monkeypatch.setattr("tvpet_console.pose_review.os.fsync", fail_sync)
    with pytest.raises(OSError, match="flush failure"):
        ReviewRecorder(str(tmp_path)).save(jpeg(), fixture())
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.parametrize("space_kind", ["cap", "free"])
def test_capacity_reserves_all_three_files_before_writing(tmp_path, monkeypatch, space_kind):
    freeze_time(monkeypatch)
    probe = tmp_path / "probe"
    ReviewRecorder(str(probe)).save(jpeg(), fixture())
    required = sum(path.stat().st_size for path in probe.rglob("*") if path.is_file())
    target = tmp_path / "target"
    target.mkdir()
    previous = target / "previous.jpg"
    previous.write_bytes(b"keep this previous image")
    recorder = ReviewRecorder(str(target))
    if space_kind == "cap":
        recorder.max_bytes = required + previous.stat().st_size - 1
    else:
        monkeypatch.setattr(
            "tvpet_console.pose_review.shutil.disk_usage",
            lambda root: SimpleNamespace(free=recorder.min_free + required - 1),
        )
    with pytest.raises(OSError, match="上限"):
        recorder.save(jpeg(), fixture())
    assert previous.read_bytes() == b"keep this previous image"
    assert [path for path in target.rglob("*") if path.is_file()] == [previous]


@pytest.mark.parametrize(
    "link_kind", ["root", "originals", "original", "annotated", "json", "temp"]
)
def test_symlink_targets_are_rejected_without_changing_target(tmp_path, monkeypatch, link_kind):
    stamp = freeze_time(monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected"
    protected.write_bytes(b"do not change")
    root = tmp_path / "review"
    if link_kind == "root":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        if link_kind == "originals":
            (root / "originals").symlink_to(outside, target_is_directory=True)
        else:
            originals = root / "originals"
            originals.mkdir()
            link = {
                "original": originals / f"{stamp}.jpg",
                "annotated": root / f"{stamp}.jpg",
                "json": root / f"{stamp}.json",
                "temp": originals / f".{stamp}.jpg.tmp",
            }[link_kind]
            link.symlink_to(protected)
    with pytest.raises((ValueError, FileExistsError)):
        ReviewRecorder(str(root)).save(jpeg(), fixture())
    assert protected.read_bytes() == b"do not change"
    assert list(outside.iterdir()) == [protected]
    if link_kind != "root":
        assert not [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()]


def test_same_stamp_does_not_overwrite_existing_files(tmp_path, monkeypatch):
    freeze_time(monkeypatch)
    recorder = ReviewRecorder(str(tmp_path))
    recorder.save(jpeg(), fixture())
    existing = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(FileExistsError, match="不覆盖"):
        recorder.save(jpeg(), fixture())
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == existing


def test_oversized_original_rejected_before_files_created(tmp_path):
    root = tmp_path / "review"
    with pytest.raises(ValueError, match="大小超出"):
        ReviewRecorder(str(root)).save(b"x" * (MAX_JPEG + 1), fixture())
    assert not root.exists()


def test_background_sample_interval_stale_failure_recovery(tmp_path, monkeypatch):
    async def scenario():
        recorder = ReviewRecorder(str(tmp_path), interval=30)
        task = asyncio.create_task(recorder.run())
        try:
            recorder.submit(jpeg(), fixture())
            for _ in range(100):
                if recorder.saved:
                    break
                await asyncio.sleep(0.01)
            assert recorder.saved == 1
            recorder.submit(jpeg(), fixture())
            await asyncio.sleep(0.03)
            assert recorder.saved == 1
            recorder.next_at = 0
            recorder.latest = (time.monotonic() - 2, jpeg(), fixture())
            recorder.event.set()
            await asyncio.sleep(0.03)
            assert recorder.saved == 1
            recorder.max_bytes = 1
            recorder.submit(jpeg(), fixture())
            for _ in range(100):
                if recorder.state == "paused":
                    break
                await asyncio.sleep(0.01)
            assert recorder.state == "paused" and recorder.status()["error"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_background_lease_survives_browser_close_and_revoke():
    async def scenario():
        gateway = Gateway(background_video=True)
        gateway.control = AsyncMock()
        gateway.state_at = time.monotonic()
        gateway.rpc = AsyncMock(return_value={"ok": True})
        await gateway.viewer("video", 1)
        await gateway.viewer("video", -1)
        assert gateway.faces.active
        assert gateway.rpc.call_args.args[1]["enabled"]
        await gateway.revoke()
        assert gateway.faces.active
        task = asyncio.create_task(gateway.background_capture())
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        gateway.rpc.assert_any_call("media.set", {"channel": "video", "enabled": True})

    asyncio.run(scenario())


def test_disconnect_race_does_not_kill_background_capture():
    async def scenario():
        gateway = Gateway(background_video=True)
        gateway.control = AsyncMock()
        gateway.state_at = time.monotonic()
        gateway.control.send_json.side_effect = RuntimeError("closed")
        with pytest.raises(ValueError, match="连接已断开"):
            await gateway.rpc("media.set", {"channel": "video", "enabled": True})
        assert not gateway.pending
        task = asyncio.create_task(gateway.background_capture())
        await asyncio.sleep(0.02)
        assert not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gateway.revoke()

    asyncio.run(scenario())


@pytest.mark.parametrize("face_only", [False, True])
def test_presence_gate_accepts_body_or_face(tmp_path, face_only):
    record = fixture()
    if face_only:
        record["pose"]["people"] = []
    else:
        record["faces"] = []
        record["recognition"] = "unavailable"
    recorder = ReviewRecorder(str(tmp_path))
    assert recorder.has_person(record)
    recorder.save(jpeg(), record)
    assert len(list(tmp_path.glob("*.jpg"))) == 1


def test_empty_frames_clear_pending_sample_without_using_interval(tmp_path):
    async def scenario():
        recorder = ReviewRecorder(str(tmp_path))
        initial_deadline = recorder.next_at
        empty = {
            "sequence": 1,
            "pose": {"state": "active", "people": []},
            "recognition": "active",
            "faces": [],
        }
        recorder.submit(jpeg(), fixture())
        recorder.submit(jpeg(), empty)
        assert recorder.latest is None and not recorder.event.is_set()
        assert recorder.state == "waiting_person"
        with pytest.raises(ValueError, match="未检测到人"):
            recorder.save(jpeg(), empty)
        task = asyncio.create_task(recorder.run())
        try:
            for _ in range(3):
                recorder.submit(jpeg(), empty)
                await asyncio.sleep(0.01)
            assert recorder.saved == 0 and recorder.next_at == initial_deadline
            assert not list(tmp_path.glob("*.jpg"))
            recorder.submit(jpeg(), fixture())
            for _ in range(100):
                if recorder.saved:
                    break
                await asyncio.sleep(0.01)
            assert recorder.saved == 1
            deadline = recorder.next_at
            recorder.submit(jpeg(), empty)
            recorder.submit(jpeg(), fixture())
            await asyncio.sleep(0.03)
            assert recorder.saved == 1 and recorder.next_at == deadline
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_unavailable_results_do_not_count_as_current_person():
    record = fixture()
    record["pose"]["state"] = "unavailable"
    record["recognition"] = "unavailable"
    assert not ReviewRecorder.has_person(record)
