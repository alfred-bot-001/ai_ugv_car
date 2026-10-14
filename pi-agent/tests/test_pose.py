"""Synthetic geometry, temporal evidence, conservative tracking and bounded IPC."""

import asyncio
import copy
import sys
from unittest.mock import AsyncMock

import pytest
from tvpet_pi.pose import MotionTrack, PoseTracker, angle, posture
from tvpet_pi.pose_process import PoseProcess
from tvpet_pi.protocol import PoseResult


def standing():
    points = [[0.5, 0.3, 0, 0] for _ in range(33)]
    for i, x, y in [
        (0, 0.5, 0.1),
        (11, 0.4, 0.25),
        (12, 0.6, 0.25),
        (23, 0.4, 0.5),
        (24, 0.6, 0.5),
        (25, 0.4, 0.7),
        (26, 0.6, 0.7),
        (27, 0.4, 0.9),
        (28, 0.6, 0.9),
        (15, 0.3, 0.6),
        (16, 0.7, 0.6),
    ]:
        points[i] = [x, y, 0, 0.99]
    return points


def test_postures_and_visibility():
    points = standing()
    assert posture(points)[0] == "standing"
    assert angle([0, 0], [0, 0], [0, 0]) == 0
    sitting = copy.deepcopy(points)
    sitting[25][:2], sitting[27][:2] = [0.6, 0.5], [0.6, 0.8]
    sitting[26][3] = 0
    assert posture(sitting)[0] == "sitting"
    squatting = copy.deepcopy(sitting)
    squatting[27][:2] = [0.45, 0.7]
    assert posture(squatting)[0] == "squatting"
    lying = [[y, x, z, v] for x, y, z, v in points]
    assert posture(lying)[0] == "lying"
    lying[25][:2], lying[27][:2] = [0.3, 0.5], [0.4, 0.4]
    lying[26][3] = 0
    assert posture(lying)[0] == "curled"
    points[11][3] = 0
    assert posture(points) == ("unknown", 0)
    points = standing()
    points[25][3] = points[26][3] = 0
    assert posture(points) == ("unknown", 0)


def test_stability_wave_and_tracking_expiry():
    points = standing()
    tracker = PoseTracker()
    for t in (0, 0.2, 0.4):
        result = tracker.update([points], t)[0]
    assert result["posture"] == "standing" and result["track_id"] == 1
    points[15][:2] = [0.2, 0.1]
    assert tracker.update([points], 0.6)[0]["action"] == "raised_hand"
    for t, x in zip((0.8, 1, 1.2, 1.4, 1.6), (0.3, 0.5, 0.3, 0.1, 0.4), strict=True):
        points[15][0] = x
        result = tracker.update([points], t)[0]
    assert result["action"] == "waving"
    result = tracker.update([points], 4)[0]
    assert result["track_id"] == 2 and result["action"] != "waving"
    assert tracker.update([], 4.2) == []
    result = tracker.update([points, points], 4.4)
    assert result[0]["track_id"] != result[1]["track_id"]


def shifted(points, dx):
    result = copy.deepcopy(points)
    for point in result:
        point[0] += dx
    return result


@pytest.mark.parametrize("expected", ["standing", "sitting", "lying"])
def test_single_person_recovers_posture_after_nearby_person_leaves(expected):
    points = standing()
    if expected == "sitting":
        points[25][:2], points[27][:2] = [0.6, 0.5], [0.6, 0.8]
        points[26][3] = 0
    elif expected == "lying":
        points = [[y, x, z, v] for x, y, z, v in points]
    tracker = PoseTracker()
    for now in (0, 0.2, 0.4):
        initial = tracker.update([points], now)[0]
    assert initial["posture"] == expected
    if expected == "standing":
        assert initial["posture_label"] == "站着"
    pair = tracker.update([points, shifted(points, 0.05)], 0.6)
    assert all(person["posture"] == "unknown" for person in pair)
    assert all(person["track_id"] != initial["track_id"] for person in pair)
    assert len(tracker.tracks) == 2
    recovering_ids = []
    for now in (0.8, 1, 1.2):
        result = tracker.update([points], now)[0]
        recovering_ids.append(result["track_id"])
    assert result["posture"] == expected
    assert len(set(recovering_ids)) == 1 and len(tracker.tracks) <= 2
    for index in range(100):
        result = tracker.update([points], 1.4 + index * 0.2)[0]
        assert result["posture"] == expected and result["track_id"] == recovering_ids[0]
        assert len(tracker.tracks) <= 2
        if index >= 2:
            assert len(tracker.tracks) == 1


@pytest.mark.parametrize("reverse_order", [False, True])
def test_shared_candidate_resets_every_person_without_inheriting_wave(reverse_order):
    points = standing()
    tracker = PoseTracker()
    for now in (0, 0.2, 0.4):
        tracker.update([points], now)
    points[15][:2] = [0.2, 0.1]
    tracker.update([points], 0.6)
    for now, x in zip((0.8, 1, 1.2, 1.4, 1.6), (0.3, 0.5, 0.3, 0.1, 0.4), strict=True):
        points[15][0] = x
        result = tracker.update([points], now)[0]
    assert result["action"] == "waving"
    old_id = result["track_id"]
    pair = [points, shifted(points, 0.05)]
    if reverse_order:
        pair.reverse()
    for index in range(30):
        results = tracker.update(pair, 1.8 + index * 0.2)
        assert len({person["track_id"] for person in results}) == 2
        assert all(person["track_id"] != old_id for person in results)
        assert all(person["action"] != "waving" for person in results)
        assert all(
            person["posture"] == ("unknown" if index < 2 else "standing") for person in results
        )
        assert len(tracker.tracks) == 2


def test_two_tracks_cross_then_separate_without_sharing_history():
    left, right = shifted(standing(), -0.15), shifted(standing(), 0.15)
    tracker = PoseTracker()
    for now in (0, 0.2, 0.4):
        before = tracker.update([left, right], now)
    assert all(person["posture"] == "standing" for person in before)
    old_ids = {person["track_id"] for person in before}
    left[15][:2] = [0.2, 0.1]
    tracker.update([left, right], 0.6)
    for now, x in zip((0.8, 1, 1.2, 1.4, 1.6), (0.25, 0.45, 0.25, 0.05, 0.35), strict=True):
        left[15][0] = x
        waving = tracker.update([left, right], now)
    assert [person["action"] for person in waving] == ["waving", "none"]
    middle = [shifted(standing(), -0.005), shifted(standing(), 0.005)]
    crossed = tracker.update(middle, 1.8)
    assert all(person["posture"] == "unknown" for person in crossed)
    assert all(person["action"] == "none" for person in crossed)
    assert all(person["track_id"] not in old_ids for person in crossed)
    assert len(tracker.tracks) == 2
    recovered_ids = []
    for now in (2, 2.2, 2.4):
        separated = tracker.update([left, right], now)
        recovered_ids.append([person["track_id"] for person in separated])
    assert all(person["posture"] == "standing" for person in separated)
    assert recovered_ids[0] == recovered_ids[1] == recovered_ids[2]
    assert len(tracker.tracks) <= 4


def test_close_people_keep_separate_postures_and_hand_history():
    first = standing()
    second = shifted(standing(), 0.05)
    second[25][:2], second[27][:2] = [0.65, 0.5], [0.65, 0.8]
    second[26][3] = 0
    tracker = PoseTracker()
    for now in (0, 0.2, 0.4):
        people = tracker.update([first, second], now)
    assert [person["posture"] for person in people] == ["standing", "sitting"]
    ids = [person["track_id"] for person in people]
    first[15][:2] = [0.2, 0.1]
    tracker.update([first, second], 0.6)
    for now, x in zip((0.8, 1, 1.2, 1.4, 1.6), (0.3, 0.5, 0.3, 0.1, 0.4), strict=True):
        first[15][0] = x
        people = tracker.update([first, second], now)
    assert [person["track_id"] for person in people] == ids
    assert [person["posture"] for person in people] == ["standing", "sitting"]
    assert [person["action"] for person in people] == ["waving", "none"]


def test_single_person_between_two_old_tracks_restarts_once_then_recovers():
    tracker = PoseTracker()
    for now in (0, 0.2, 0.4):
        previous = tracker.update([shifted(standing(), -0.05), shifted(standing(), 0.05)], now)
    old_ids = {person["track_id"] for person in previous}
    recovering = []
    for now in (0.6, 0.8, 1):
        person = tracker.update([standing()], now)[0]
        recovering.append(person["track_id"])
        assert person["track_id"] not in old_ids
        assert len(tracker.tracks) == 1
    assert len(set(recovering)) == 1 and person["posture"] == "standing"


@pytest.mark.parametrize("distance", [0.05, 0.1])
@pytest.mark.parametrize("expected", ["standing", "sitting", "lying"])
def test_stable_close_people_preserve_clear_nearest_tracks(distance, expected):
    points = standing()
    if expected == "sitting":
        points[25][:2], points[27][:2] = [0.6, 0.5], [0.6, 0.8]
        points[26][3] = 0
    elif expected == "lying":
        points = [[y, x, z, v] for x, y, z, v in points]
    tracker = PoseTracker()
    first_ids = None
    for index in range(30):
        people = tracker.update([points, shifted(points, distance)], index * 0.2)
        ids = [person["track_id"] for person in people]
        if first_ids is None:
            first_ids = ids
        assert ids == first_ids and len(set(ids)) == 2
        assert all(person["posture"] == ("unknown" if index < 2 else expected) for person in people)
        assert len(tracker.tracks) == 2


def test_equal_centers_remain_ambiguous_without_track_accumulation():
    tracker = PoseTracker()
    for index in range(30):
        people = tracker.update([standing(), standing()], index * 0.2)
        assert len({person["track_id"] for person in people}) == 2
        assert all(person["posture"] == "unknown" for person in people)
        assert len(tracker.tracks) == 2


def test_brief_missing_detection_keeps_track_but_one_second_gap_expires():
    tracker = PoseTracker()
    for now in (0, 0.2, 0.4):
        initial = tracker.update([standing()], now)[0]
    assert tracker.update([], 0.6) == []
    returned = tracker.update([standing()], 0.8)[0]
    assert returned["track_id"] == initial["track_id"] and returned["posture"] == "standing"
    assert tracker.update([], 1.4) == []
    expired = tracker.update([standing()], 1.80001)[0]
    assert expired["track_id"] != initial["track_id"] and expired["posture"] == "unknown"


def test_static_hand_and_camera_translation_are_not_waves():
    track = MotionTrack(1, 0, (0.5, 0.5))
    for i in range(12):
        points = standing()
        points[15][:2] = [0.4, 0.1]
        for point in points:
            point[0] += 0.08 if i % 2 else -0.08
        result = track.classify(points, i * 0.2)
        assert result["action"] == "raised_hand"
    points[15][3] = 0
    assert track.classify(points, 3)["action"] == "none"


def test_pose_contract_rejects_invalid_unbounded_data():
    result = PoseTracker().update([standing()], 0)
    assert PoseResult.model_validate({"state": "active", "people": result}).people
    result[0]["keypoints"][0][0] = float("nan")
    with pytest.raises(ValueError):
        PoseResult.model_validate({"state": "active", "people": result})
    with pytest.raises(ValueError):
        PoseResult.model_validate({"state": "active", "people": [{}] * 4})


def test_process_initialization_inference_failure_and_retry(monkeypatch):
    async def scenario():
        worker = PoseProcess()
        assert (await worker.infer(b"x"))["state"] == "disabled"
        actual = asyncio.create_subprocess_exec
        program = (
            "import sys,json; print(json.dumps({'ready':True}),flush=True); "
            "sys.stdin.buffer.read(5); "
            "print(json.dumps({'state':'active','people':[],'inference_ms':12}),flush=True)"
        )

        async def launch(*args, **kwargs):
            return await actual(sys.executable, "-c", program, **kwargs)

        monkeypatch.setattr("tvpet_pi.pose_process.asyncio.create_subprocess_exec", launch)
        worker = PoseProcess("python", "model")
        assert (await worker.infer(b"x"))["state"] == "active"
        assert worker.inference_ms == 12
        assert (await worker.infer(b"x"))["state"] == "unavailable"
        assert worker.process is None
        assert (await worker.infer(b"x"))["state"] == "unavailable"
        worker.retry_at = 0
        monkeypatch.setattr(
            "tvpet_pi.pose_process.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=OSError("missing")),
        )
        assert (await worker.infer(b"x"))["state"] == "unavailable"
        await worker.stop()

    asyncio.run(scenario())
