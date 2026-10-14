"""Local skeleton rules and short-lived tracking; no identity or actuator access."""

import math
from collections import Counter, deque
from typing import Any

CONNECTIONS = (
    (11, 12),
    (11, 13),
    (13, 15),
    (12, 14),
    (14, 16),
    (11, 23),
    (12, 24),
    (23, 24),
    (23, 25),
    (25, 27),
    (24, 26),
    (26, 28),
    (27, 29),
    (29, 31),
    (28, 30),
    (30, 32),
)
LABELS = {
    "standing": "站着",
    "sitting": "坐着",
    "lying": "躺着",
    "squatting": "蹲着",
    "curled": "蜷缩（候选）",
    "unknown": "无法判断",
    "raised_hand": "举手",
    "waving": "挥手打招呼",
    "none": "无明显动作",
}


def visible(points: list[list[float]], indices: tuple[int, ...]) -> bool:
    return all(
        points[i][3] >= 0.6 and 0 <= points[i][0] <= 1 and 0 <= points[i][1] <= 1 for i in indices
    )


def angle(a: list[float], b: list[float], c: list[float]) -> float:
    u, v = ((a[0] - b[0]) * 4 / 3, a[1] - b[1]), ((c[0] - b[0]) * 4 / 3, c[1] - b[1])
    norm = math.hypot(*u) * math.hypot(*v)
    return (
        math.degrees(math.acos(max(-1, min(1, (u[0] * v[0] + u[1] * v[1]) / norm))))
        if norm > 1e-6
        else 0
    )


def posture(points: list[list[float]]) -> tuple[str, float]:
    """Require visible joints; geometric heuristics are not calibrated probabilities."""
    if not visible(points, (11, 12, 23, 24)):
        return "unknown", 0.0
    shoulder = [(points[11][i] + points[12][i]) / 2 for i in range(2)]
    hip = [(points[23][i] + points[24][i]) / 2 for i in range(2)]
    dx, dy = abs(hip[0] - shoulder[0]), abs(hip[1] - shoulder[1])
    if max(dx, dy) < 0.035:
        return "unknown", 0.0
    sides = [(11, 23, 25, 27), (12, 24, 26, 28)]
    legs = [side for side in sides if visible(points, side)]
    if not legs:
        return "unknown", 0.0
    sh, h, k, a = max(legs, key=lambda ids: min(points[i][3] for i in ids))
    knee = angle(points[h], points[k], points[a])
    hip_angle = angle(points[sh], points[h], points[k])
    confidence = min(points[i][3] for i in (sh, h, k, a))
    if dx > dy * 1.4:
        return ("curled" if knee < 90 and hip_angle < 80 else "lying"), confidence
    if dy < dx * 0.8 or hip[1] < shoulder[1]:
        return "unknown", 0.0
    if knee > 150 and hip_angle > 145:
        return "standing", confidence
    if knee < 85 and points[h][1] >= points[k][1] - 0.04:
        return "squatting", confidence
    if knee < 145 and hip_angle < 140:
        return "sitting", confidence
    return "unknown", 0.0


class MotionTrack:
    def __init__(self, track_id: int, now: float, center: tuple[float, float]) -> None:
        self.track_id, self.seen, self.center = track_id, now, center
        self.samples: deque[tuple[float, str]] = deque(maxlen=20)
        self.hands: dict[int, deque[tuple[float, float]]] = {
            15: deque(maxlen=30),
            16: deque(maxlen=30),
        }
        self.wave_until = 0.0
        self.cooldown_until = 0.0

    def classify(self, points: list[list[float]], now: float) -> dict[str, Any]:
        pose, confidence = posture(points)
        self.samples.append((now, pose))
        recent = [(t, p) for t, p in self.samples if now - t <= 0.8]
        stable = (
            len(recent) >= 3
            and now - recent[0][0] >= 0.35
            and sum(p == pose for _, p in recent) / len(recent) >= 0.75
        )
        if not stable:
            pose, confidence = "unknown", 0.0
        raised = False
        for wrist, shoulder, opposite in ((15, 11, 12), (16, 12, 11)):
            samples = self.hands[wrist]
            if (
                not visible(points, (wrist, shoulder, opposite))
                or points[wrist][1] >= points[shoulder][1] - 0.03
            ):
                samples.clear()
                continue
            raised = True
            scale = max(0.08, math.dist(points[shoulder][:2], points[opposite][:2]))
            x = (points[wrist][0] - points[shoulder][0]) / scale
            samples.append((now, x))
            while samples and now - samples[0][0] > 2:
                samples.popleft()
            if len(samples) < 5 or now - samples[0][0] < 0.5:
                continue
            anchor, direction, turns = samples[0][1], 0, 0
            for _, value in samples:
                delta = value - anchor
                if abs(delta) < 0.25:
                    continue
                step = 1 if delta > 0 else -1
                if direction and step != direction:
                    turns += 1
                anchor, direction = value, step
            if turns >= 2 and now >= self.cooldown_until:
                self.wave_until, self.cooldown_until = now + 1, now + 3
                samples.clear()
        action = "waving" if now < self.wave_until else "raised_hand" if raised else "none"
        return {
            "track_id": self.track_id,
            "posture": pose,
            "posture_label": LABELS[pose],
            "score": round(confidence, 3),
            "action": action,
            "action_label": LABELS[action],
            "keypoints": points,
        }


class PoseTracker:
    def __init__(self) -> None:
        self.tracks: list[MotionTrack] = []
        self.next_id = 1

    def update(self, people: list[list[list[float]]], now: float) -> list[dict[str, Any]]:
        previous = [track for track in self.tracks if now - track.seen < 1]
        centers = [
            (
                (points[11][0] + points[12][0] + points[23][0] + points[24][0]) / 4,
                (points[11][1] + points[12][1] + points[23][1] + points[24][1]) / 4,
            )
            for points in people
        ]
        # Match against one old snapshot, before any new track or moved center exists.
        candidates: list[list[MotionTrack]] = []
        for center in centers:
            nearby = sorted(
                (
                    (math.dist(center, track.center), track)
                    for track in previous
                    if math.dist(center, track.center) < 0.18
                ),
                key=lambda item: item[0],
            )
            # Clearly closer matches preserve stable neighbors, even inside the radius.
            # Near-equal alternatives remain ambiguous and cannot reuse temporal state.
            candidates.append(
                [track for distance, track in nearby if distance - nearby[0][0] <= 0.025]
            )
        references = Counter(track.track_id for options in candidates for track in options)
        ambiguous = {
            track.track_id
            for options in candidates
            if len(options) != 1 or references[options[0].track_id] != 1
            for track in options
        }
        active: list[MotionTrack] = []
        output = []
        for points, center, options in zip(people, centers, candidates, strict=True):
            if len(options) == 1 and options[0].track_id not in ambiguous:
                track = options[0]
            else:
                # Crossings cannot inherit another person's posture or wave history.
                track = MotionTrack(self.next_id, now, center)
                self.next_id += 1
            track.seen, track.center = now, center
            active.append(track)
            output.append(track.classify(points, now))
        active_ids = {track.track_id for track in active}
        missing = sorted(
            (
                track
                for track in previous
                if track.track_id not in active_ids and track.track_id not in ambiguous
            ),
            key=lambda track: track.seen,
            reverse=True,
        )
        # Retire ambiguous history immediately, so it cannot cause endless resets.
        # Keep short occlusions, with a hard state bound beyond the worker's three people.
        self.tracks = active + missing[: max(0, 16 - len(active))]
        return output
