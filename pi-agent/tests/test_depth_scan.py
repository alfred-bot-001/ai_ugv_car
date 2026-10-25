"""Depth to virtual scan: floor fit and obstacle bands on ray-cast synthetic scenes."""

import math

import numpy as np
import pytest
from tvpet_pi.depth_scan import (
    BINS,
    CAMERA_X_M,
    CX,
    CY,
    FX,
    FY,
    HEIGHT,
    WIDTH,
    Floor,
    combine_floors,
    fit_floor,
    virtual_scan,
)

LENS_M = 0.138  # the Gemini sits on the frame 138 mm above the floor


def render(
    pitch_deg: float = 4.0,
    boxes: tuple[tuple[float, float, float, float, float, float], ...] = (),
    height: float = LENS_M,
) -> np.ndarray:
    """Depth (mm) seen by a lens `height` up, pitched down; boxes are world AABBs.

    World axes: X ahead along the floor from below the lens, Y left, Z up. Each box is
    (x0, x1, y0, y1, z0, z1). The floor is Z = 0.
    """
    pitch = math.radians(pitch_deg)
    ahead = np.array([math.cos(pitch), 0.0, -math.sin(pitch)])  # camera z
    down = np.array([-math.sin(pitch), 0.0, -math.cos(pitch)])  # camera y
    right = np.array([0.0, -1.0, 0.0])  # camera x
    v, u = np.mgrid[0:HEIGHT, 0:WIDTH].astype(np.float64)
    rays = (
        ((u - CX) / FX)[..., None] * right
        + ((v - CY) / FY)[..., None] * down
        + ahead  # camera-frame z of every ray is 1, so the hit distance t is the depth
    )
    origin = np.array([0.0, 0.0, height])
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(rays[..., 2] < 0, -height / rays[..., 2], np.inf)
        for x0, x1, y0, y1, z0, z1 in boxes:
            low = (np.array([x0, y0, z0]) - origin) / rays
            high = (np.array([x1, y1, z1]) - origin) / rays
            near = np.nanmax(np.minimum(low, high), axis=-1)
            far = np.nanmin(np.maximum(low, high), axis=-1)
            hit = (near <= far) & (near > 0)
            t = np.where(hit & (near < t), near, t)
    depth = np.where(np.isfinite(t) & (t < 8), np.round(t * 1000), 0)
    return depth.astype(np.uint16)


def bearing_bin(degrees: float) -> int:
    return int((math.radians(degrees) + math.radians(35)) // math.radians(1))


@pytest.mark.parametrize("pitch", [0.0, 4.0, 8.0])
def test_floor_fit_recovers_height_and_pitch_and_round_trips(pitch: float) -> None:
    floor = fit_floor(render(pitch_deg=pitch))
    assert floor.height == pytest.approx(LENS_M, abs=0.003)
    data = floor.to_json()
    assert data["pitch_down_deg"] == pytest.approx(pitch, abs=0.2)
    assert abs(data["roll_deg"]) < 0.2 and data["camera_x_m"] == CAMERA_X_M
    assert Floor.from_json(data).height == pytest.approx(floor.height, abs=1e-4)
    for bad in (
        {},
        {**data, "schema": 2},
        {**data, "height_m": 0.9},
        {**data, "normal": [0, 0]},
        {**data, "normal": None},
        {**data, "camera_x_m": "far"},
    ):
        with pytest.raises(ValueError):
            Floor.from_json(bad)


def test_floor_fit_refuses_a_blocked_view() -> None:
    wall = render(boxes=((0.45, 0.6, -3, 3, 0, 2),))  # a wall 45 cm ahead fills the view
    with pytest.raises(ValueError, match="floor|readings"):
        fit_floor(wall)


def test_bare_floor_reads_clear_everywhere() -> None:
    floor = fit_floor(render())
    scan = virtual_scan(render(), floor)
    assert len(scan["ranges_mm"]) == BINS and scan["range_max"] == 2.0
    assert scan["angle_min"] == pytest.approx(-math.radians(34.5), abs=1e-6)
    assert set(scan["ranges_mm"]) <= {0, None} and scan["ranges_mm"].count(0) > BINS * 0.8


def test_low_box_and_overhang_are_seen_low_bump_and_far_wall_are_not() -> None:
    floor = fit_floor(render())
    scene = render(
        boxes=(
            (0.80, 0.95, -0.10, 0.10, 0.0, 0.10),  # a 10 cm toy dead ahead
            (0.90, 1.40, 0.45, 0.80, 0.22, 0.30),  # a table edge 22-30 cm up, to the left
            (0.60, 0.80, -0.70, -0.40, 0.0, 0.03),  # a 3 cm rug edge, to the right
            (1.75, 1.85, -3, 3, 0.0, 1.0),  # a wall beyond the reported range
        )
    )
    ranges = virtual_scan(scene, floor)["ranges_mm"]
    ahead = ranges[bearing_bin(0)]
    assert ahead == pytest.approx((0.80 + CAMERA_X_M) * 1000, abs=25)
    # The table edge spans bearings 16-38 deg; its nearest corner is 1.12 m away.
    table = [r for r in ranges[bearing_bin(16) :] if r]
    assert min(table) == pytest.approx(math.hypot(0.90 + CAMERA_X_M, 0.45) * 1000, abs=30)
    rug = ranges[bearing_bin(-36 + 1) : bearing_bin(-25)]
    assert all(r in (0, None) for r in rug)  # 3 cm is floor noise; the far wall is beyond
    marked = [i for i, r in enumerate(ranges) if r]
    assert all(bearing_bin(-8) <= i <= bearing_bin(8) or bearing_bin(16) <= i for i in marked)


def test_bearings_without_readings_are_unknown() -> None:
    floor = fit_floor(render())
    blind = render()
    blind[:, 280:360] = 0  # a black object returns nothing
    ranges = virtual_scan(blind, floor)["ranges_mm"]
    assert ranges[bearing_bin(0)] is None and ranges[bearing_bin(-20)] == 0


def test_calibration_frames_must_agree() -> None:
    floors = [fit_floor(render(pitch_deg=p)) for p in (3.8, 4.0, 4.2)]
    floor = combine_floors(floors)
    assert floor.to_json()["pitch_down_deg"] == pytest.approx(4.0, abs=0.1)
    with pytest.raises(ValueError, match="disagree"):
        combine_floors([floors[0], fit_floor(render(pitch_deg=6.0))])
    with pytest.raises(ValueError):
        combine_floors([])
