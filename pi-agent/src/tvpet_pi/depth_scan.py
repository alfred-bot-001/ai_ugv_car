"""Front depth as a virtual laser scan for the Nav2 costmaps (numpy; runs in pose-venv).

The floor plane comes from a one-time calibration (`fit_floor`, saved by
`tvpet_pi.depth_worker --calibrate`). Each frame becomes points; those between
MIN_HEIGHT_M and MAX_HEIGHT_M above that floor are what the LiDAR's single plane can
miss: toys, shoes and thresholds below it, table and sofa edges above it. Per bearing
from base_link, the range is the nearest such point. A bearing that saw enough of the
scene but nothing in that band reads 0 (clear); one with too few readings reads None
(unknown: it neither clears nor marks). Cliffs (points below the floor) are not
reported; the balcony is a keepout zone and the ToF sensor watches the floor ahead.

Only the standard library and numpy: tvpet_pi.depth_worker imports this in pose-venv.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np

# Gemini depth at 640 x 400. The driver reports fx = fy = 945.028 and c = (640, 400),
# which is the full 1280 x 800 sensor; halved, they give the specified 68 x 46 deg view.
FX = FY = 945.028 / 2
CX, CY = 320.0, 200.0
WIDTH, HEIGHT = 640, 400
STEP = 2  # every other row and column: 320 x 200 points a frame
# Height band of obstacles above the floor (m), 2026-10-25. Above 5 cm: floor noise and a
# 1 deg pitch error stay below it out to MAX_RANGE_M. Below 35 cm: the car with its
# gimbal is about 25 cm tall, so anything lower than that plus a margin is in the way.
MIN_HEIGHT_M = 0.05
MAX_HEIGHT_M = 0.35
# Obstacles are reported out to 1.5 m (a 1 deg pitch error is 2.6 cm of height there);
# a clear bearing clears out to 2 m (the scan's range_max).
MAX_RANGE_M = 1.5
CLEAR_RANGE_M = 2.0
HALF_FOV = math.radians(35.0)
BIN = math.radians(1.0)
BINS = round(2 * HALF_FOV / BIN)
MIN_POINTS = 8  # in-band points a bearing needs to count as an obstacle
NEAREST_RANK = 3  # its range is the 3rd nearest point: one or two stray pixels do not count
MIN_SEEN = 20  # readings a bearing needs to count as clear
# Lens ahead of base_link (m): an estimate from the mount, checked against the LiDAR.
CAMERA_X_M = 0.13

# Floor fit: points 0.3-2.5 m away in the lower half of the image, RANSAC then a least
# squares refit. A floor that tilts more than 35 deg from the camera's "up", sits outside
# 5-40 cm below the lens, explains under 60 % of those points or spans under 0.5 m of
# distance (a wall close ahead leaves a strip of floor that any tilt fits) is refused.
FIT_MIN_ROW = 200  # the optical centre: the floor lies below it unless pitched far down
FIT_RANGE_M = (0.3, 2.5)
FIT_TRIALS = 300
FIT_TOLERANCE_M = 0.015
FIT_MAX_TILT = math.radians(35.0)
FIT_HEIGHT_M = (0.05, 0.40)
FIT_MIN_SHARE = 0.6
FIT_MIN_SPAN_M = 0.5


@dataclass(frozen=True)
class Floor:
    """The floor in the depth camera's frame (x right, y down, z ahead)."""

    normal: tuple[float, float, float]  # unit, pointing up from the floor
    height: float  # lens above the floor (m)
    camera_x: float = CAMERA_X_M

    def to_json(self) -> dict[str, Any]:
        nx, ny, nz = self.normal
        return {
            "schema": 1,
            "normal": [round(v, 6) for v in self.normal],
            "height_m": round(self.height, 4),
            "camera_x_m": round(self.camera_x, 3),
            # For people reading the file: lens pitch down and roll (deg).
            "pitch_down_deg": round(math.degrees(math.asin(max(-1.0, min(1.0, -nz)))), 2),
            "roll_deg": round(math.degrees(math.atan2(nx, -ny)), 2),
        }

    @classmethod
    def from_json(cls, value: Any) -> Floor:
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise ValueError("not a depth floor calibration")
        normal = value.get("normal")
        if not isinstance(normal, list) or len(normal) != 3:
            raise ValueError("depth floor calibration is incomplete")
        raw = [*normal, value.get("height_m"), value.get("camera_x_m", CAMERA_X_M)]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in raw):
            raise ValueError("depth floor calibration is incomplete")
        nx, ny, nz, height, camera_x = (float(v) for v in raw)
        if (
            abs(math.hypot(nx, ny, nz) - 1) > 1e-3
            or not FIT_HEIGHT_M[0] <= height <= FIT_HEIGHT_M[1]
        ):
            raise ValueError("depth floor calibration is out of range")
        if not 0.0 <= camera_x <= 0.4:
            raise ValueError("depth camera_x_m is out of range")
        return cls((nx, ny, nz), height, camera_x)


def camera_points(depth: Any, rows: slice = slice(None)) -> Any:
    """(N, 3) points in metres, camera frame, from a uint16 depth image in mm."""
    height, width = depth.shape
    sx, sy = width / WIDTH, height / HEIGHT  # intrinsics scale with the resolution
    v, u = np.mgrid[0:height:STEP, 0:width:STEP]
    z = depth[::STEP, ::STEP].astype(np.float64) / 1000.0
    v, u, z = v[rows], u[rows], z[rows]
    valid = z > 0
    z = z[valid]
    x = (u[valid] - CX * sx) * z / (FX * sx)
    y = (v[valid] - CY * sy) * z / (FY * sy)
    return np.stack([x, y, z], axis=1)


def _plane(points: Any) -> tuple[Any, float]:
    """Least-squares plane through points: unit normal (pointing up) and lens height."""
    centre = points.mean(axis=0)
    normal = np.linalg.svd(points - centre, full_matrices=False)[2][-1]
    if normal[1] > 0:  # the camera's y points down; "up" has a negative y
        normal = -normal
    return normal, float(-normal @ centre)


def fit_floor(depth: Any, seed: int = 0, camera_x: float = CAMERA_X_M) -> Floor:
    """The floor plane from a depth image with clear floor ahead; ValueError if unsure."""
    height = depth.shape[0]
    first = int(FIT_MIN_ROW * height / HEIGHT) // STEP
    points = camera_points(depth, slice(first, None))
    near, far = FIT_RANGE_M
    points = points[(points[:, 2] >= near) & (points[:, 2] <= far)]
    if len(points) < 200:
        raise ValueError(f"too few floor readings ({len(points)})")
    rng = np.random.default_rng(seed)
    best: Any = None
    best_count = 0
    for _ in range(FIT_TRIALS):
        a, b, c = points[rng.choice(len(points), 3, replace=False)]
        normal = np.cross(b - a, c - a)
        length = np.linalg.norm(normal)
        if length < 1e-9:
            continue
        normal = normal / length
        if normal[1] > 0:
            normal = -normal
        offset = float(-normal @ a)
        if -normal[1] < math.cos(FIT_MAX_TILT) or not FIT_HEIGHT_M[0] <= offset <= FIT_HEIGHT_M[1]:
            continue
        inliers = np.abs(points @ normal + offset) < FIT_TOLERANCE_M
        count = int(inliers.sum())
        if count > best_count:
            best, best_count = inliers, count
    if best is None or best_count < FIT_MIN_SHARE * len(points):
        share = best_count / len(points)
        raise ValueError(f"no clear floor ahead ({share:.0%} of the lower image fits a plane)")
    floor_points = points[best]
    span = float(floor_points[:, 2].max() - floor_points[:, 2].min())
    if span < FIT_MIN_SPAN_M:
        raise ValueError(f"no clear floor ahead (the floor seen spans only {span:.2f} m)")
    normal, offset = _plane(floor_points)
    if -normal[1] < math.cos(FIT_MAX_TILT) or not FIT_HEIGHT_M[0] <= offset <= FIT_HEIGHT_M[1]:
        raise ValueError("the floor fit is out of range")
    return Floor((float(normal[0]), float(normal[1]), float(normal[2])), offset, camera_x)


def combine_floors(floors: list[Floor]) -> Floor:
    """One floor from several frames' fits; they must agree within 1 cm and 1 deg."""
    if not floors:
        raise ValueError("no floor fits")
    heights = [f.height for f in floors]
    normals = np.array([f.normal for f in floors])
    normal = normals.mean(axis=0)
    normal /= np.linalg.norm(normal)
    spread = max(math.degrees(math.acos(min(1.0, float(n @ normal)))) for n in normals)
    if max(heights) - min(heights) > 0.01 or spread > 1.0:
        raise ValueError("the floor fits disagree between frames; is something moving?")
    height = float(np.median(heights))
    return Floor((float(normal[0]), float(normal[1]), float(normal[2])), height, floors[0].camera_x)


@lru_cache(maxsize=4)
def ray_factors(height: int, width: int, floor: Floor) -> tuple[Any, Any, Any]:
    """Per sampled pixel, what one metre of depth adds to height, ahead and left.

    A pixel's point is depth times its ray, so height above the floor, distance ahead
    of the lens and distance to the left are each depth times a fixed factor (plus an
    offset). Worked out once per image size and floor; each frame then only multiplies
    (2026-10-25: rebuilding points and matrix products every frame cost 38 ms of CPU).
    """
    sx, sy = width / WIDTH, height / HEIGHT
    v, u = np.mgrid[0:height:STEP, 0:width:STEP]
    rays = np.stack(
        [(u - CX * sx) / (FX * sx), (v - CY * sy) / (FY * sy), np.ones(u.shape)], axis=-1
    ).reshape(-1, 3)
    up = np.array(floor.normal)
    ahead = np.array([0.0, 0.0, 1.0]) - up[2] * up  # the lens axis along the floor
    ahead /= np.linalg.norm(ahead)
    left = np.cross(up, ahead)
    return (
        (rays @ up).astype(np.float32),
        (rays @ ahead).astype(np.float32),
        (rays @ left).astype(np.float32),
    )


def virtual_scan(depth: Any, floor: Floor) -> dict[str, Any]:
    """One frame as a scan around base_link: ranges in mm, 0 clear, None unknown."""
    up_k, ahead_k, left_k = ray_factors(depth.shape[0], depth.shape[1], floor)
    z = depth[::STEP, ::STEP].reshape(-1).astype(np.float32) / 1000.0
    valid = z > 0
    z = z[valid]
    above = z * up_k[valid] + floor.height
    x = z * ahead_k[valid] + floor.camera_x
    y = z * left_k[valid]
    bins = np.floor((np.arctan2(y, x) + HALF_FOV) / BIN).astype(np.int64)
    inside = (bins >= 0) & (bins < BINS)
    seen = np.bincount(bins[inside], minlength=BINS)
    hit = inside & (above >= MIN_HEIGHT_M) & (above <= MAX_HEIGHT_M)
    distance = np.hypot(x, y)
    hit &= distance <= MAX_RANGE_M
    hit_bins, hit_distance = bins[hit], distance[hit]
    order = np.lexsort((hit_distance, hit_bins))
    hit_bins, hit_distance = hit_bins[order], hit_distance[order]
    counts = np.bincount(hit_bins, minlength=BINS)
    starts = np.searchsorted(hit_bins, np.arange(BINS))
    ranges: list[int | None] = []
    for i in range(BINS):
        if counts[i] >= MIN_POINTS:
            ranges.append(int(round(hit_distance[starts[i] + NEAREST_RANK - 1] * 1000)))
        elif seen[i] >= MIN_SEEN:
            ranges.append(0)
        else:
            ranges.append(None)
    return {
        "angle_min": round(-HALF_FOV + BIN / 2, 6),  # the first bearing's centre
        "angle_increment": round(BIN, 6),
        "range_max": CLEAR_RANGE_M,
        "ranges_mm": ranges,
    }
