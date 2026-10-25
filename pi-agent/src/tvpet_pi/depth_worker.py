"""Front Orbbec depth worker, run with pose-venv's Python (numpy, OpenCV, openni).

Reads the OpenNI2 depth stream (640x400, 1 mm units), and DEPTH_FPS times a second
writes a colorized JPEG with a few distances to stdout (tvpet_pi.depth_wire framing).
Exits when stdin closes, so it never outlives tvpet_pi.agent. Runs while someone watches
the depth view or Nav2 wants the scan (the sensor's laser is on only then). With a floor
calibration (--floor) each frame also carries the virtual scan (tvpet_pi.depth_scan).
pose-venv has none of the agent's dependencies, so from this package it imports only
depth_wire and depth_scan (standard library and numpy).

Floor calibration, with clear floor ahead and no depth view or navigation running:
    pose-venv/bin/python -m tvpet_pi.depth_worker --openni DIR --calibrate FILE
"""

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

import cv2
import numpy as np
from openni import _openni2 as c_api
from openni import openni2

from .depth_scan import CAMERA_X_M, Floor, combine_floors, fit_floor, virtual_scan
from .depth_wire import FAR_MM, NEAR_MM, encode_frame

CENTRE_HALF = 6  # px: the centre reading is the median of a 13 x 13 patch
CALIBRATION_SKIP = 15
CALIBRATION_FRAMES = 5


def depth_stats(depth: Any) -> dict[str, Any]:
    """Valid share, the centre distance and a robust nearest distance (mm or None)."""

    height, width = depth.shape
    valid = depth[depth > 0]
    cy, cx = height // 2, width // 2
    patch = depth[cy - CENTRE_HALF : cy + CENTRE_HALF + 1, cx - CENTRE_HALF : cx + CENTRE_HALF + 1]
    patch = patch[patch > 0]
    return {
        "width": int(width),
        "height": int(height),
        "valid": round(float(valid.size) / depth.size, 3),
        "centre_mm": int(np.median(patch)) if patch.size >= 5 else None,
        "nearest_mm": int(np.percentile(valid, 1)) if valid.size >= 100 else None,
        "near_mm": NEAR_MM,
        "far_mm": FAR_MM,
    }


def colorize(depth: Any) -> Any:
    """Blue (near) to red (far) over NEAR_MM..FAR_MM; no reading is black."""

    scaled = (np.clip(depth.astype(np.float32), NEAR_MM, FAR_MM) - NEAR_MM) * (
        255.0 / (FAR_MM - NEAR_MM)
    )
    image = cv2.applyColorMap(scaled.astype(np.uint8), cv2.COLORMAP_TURBO)
    image[depth == 0] = 0
    return image


def as_array(frame: Any) -> Any:
    depth = np.frombuffer(frame.get_buffer_as_uint16(), dtype=np.uint16)
    return depth.reshape(frame.height, frame.width)


def encode(depth: Any, floor: Floor | None) -> tuple[bytes, dict[str, Any]] | None:
    """A depth image as a colorized JPEG, its distances and (calibrated) the scan."""

    ok, jpeg = cv2.imencode(".jpg", colorize(depth), [cv2.IMWRITE_JPEG_QUALITY, 75])
    if not ok:
        return None
    meta = depth_stats(depth)
    if floor is not None:
        meta["scan"] = virtual_scan(depth, floor)
    return jpeg.tobytes(), meta


def load_floor(path: str) -> Floor | None:
    """The floor calibration, or None (none yet, or unusable: then said on stderr)."""
    if not path:
        return None
    try:
        return Floor.from_json(json.loads(Path(path).expanduser().read_text()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        print(f"depth floor calibration {path} ignored: {error}", file=sys.stderr)
        return None


def calibrate(stream: Any, path: Path, camera_x: float) -> dict[str, Any]:
    """Fit the floor on several frames; they must agree. Writes `path` (0600)."""
    for _ in range(CALIBRATION_SKIP):  # the first frames after start can be patchy
        stream.read_frame()
    floors = []
    for _ in range(CALIBRATION_FRAMES):
        for _ in range(5):  # about 6 Hz apart
            frame = stream.read_frame()
        floors.append(fit_floor(as_array(frame), camera_x=camera_x))
    data = combine_floors(floors).to_json()
    data |= {"frames": len(floors), "calibrated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(data, file, indent=1)
    os.replace(temporary, path)
    return data


def frame_output() -> IO[bytes]:
    """A private copy of stdout for frames; fd 1 itself then points at stderr.

    Orbbec's driver prints its own lines (USB priority warning, calibration) to stdout,
    which would corrupt the frame stream, so they go to the agent's journal instead.
    """
    sys.stdout.flush()
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    return out


def exit_when_stdin_closes() -> None:
    sys.stdin.buffer.read()
    os._exit(0)


def open_stream(openni: str) -> Any:
    openni2.initialize(openni)
    device = openni2.Device.open_any()
    stream = device.create_depth_stream()
    stream.set_video_mode(
        c_api.OniVideoMode(
            pixelFormat=c_api.OniPixelFormat.ONI_PIXEL_FORMAT_DEPTH_1_MM,
            resolutionX=640,
            resolutionY=400,
            fps=30,
        )
    )
    # OpenNI mirrors depth left-right by default (seen on the car 2026-10-25: the closet on
    # the colour camera's right showed on the depth view's left). The scan's bearings and
    # the depth view need the true side.
    stream.set_mirroring_enabled(False)
    stream.start()
    return stream


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openni", required=True, help="folder with libOpenNI2.so")
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument("--floor", default="", help="floor calibration; frames then carry a scan")
    parser.add_argument(
        "--calibrate", metavar="FILE", help="fit the floor ahead into FILE and exit"
    )
    parser.add_argument(
        "--camera-x", type=float, default=CAMERA_X_M, help="lens ahead of base_link (m)"
    )
    args = parser.parse_args()
    if args.calibrate:
        stream = open_stream(args.openni)
        try:
            data = calibrate(stream, Path(args.calibrate).expanduser(), args.camera_x)
            print(json.dumps(data, ensure_ascii=False))
        finally:
            stream.stop()
            openni2.unload()
        return
    period = 1.0 / max(0.5, min(args.fps, 10.0))
    cv2.setNumThreads(1)  # 5 frames a second need no thread pool (see depth.WORKER_ENV)
    floor = load_floor(args.floor)
    out = frame_output()
    threading.Thread(target=exit_when_stdin_closes, daemon=True).start()
    stream = open_stream(args.openni)
    sequence, last = 0, 0.0
    try:
        while True:
            frame = stream.read_frame()
            now = time.monotonic()
            if now - last < period:
                continue
            last = now
            encoded = encode(as_array(frame), floor)
            if encoded is None:
                continue
            jpeg, meta = encoded
            sequence += 1
            out.write(encode_frame({"sequence": sequence, **meta}, jpeg))
            out.flush()
    except BrokenPipeError:
        pass
    finally:
        stream.stop()
        openni2.unload()


if __name__ == "__main__":
    main()
