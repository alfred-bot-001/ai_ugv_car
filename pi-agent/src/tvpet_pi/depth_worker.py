"""Front Orbbec depth worker, run with pose-venv's Python (numpy, OpenCV, openni).

Reads the OpenNI2 depth stream (640x400, 1 mm units), and DEPTH_FPS times a second
writes a colorized JPEG with a few distances to stdout (tvpet_pi.depth_wire framing).
Exits when stdin closes, so it never outlives tvpet_pi.agent. Started only while someone
watches the depth view; the sensor's laser is on only then. pose-venv has none of the
agent's dependencies, so from this package it imports only depth_wire (stdlib only).
"""

import argparse
import os
import sys
import threading
import time
from typing import IO, Any

import cv2
import numpy as np  # type: ignore[import-not-found]
from openni import _openni2 as c_api
from openni import openni2

from .depth_wire import FAR_MM, NEAR_MM, encode_frame

CENTRE_HALF = 6  # px: the centre reading is the median of a 13 x 13 patch


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


def encode(frame: Any) -> tuple[bytes, dict[str, Any]] | None:
    """An OpenNI depth frame as a colorized JPEG and its distances."""

    depth = np.frombuffer(frame.get_buffer_as_uint16(), dtype=np.uint16)
    depth = depth.reshape(frame.height, frame.width)
    ok, jpeg = cv2.imencode(".jpg", colorize(depth), [cv2.IMWRITE_JPEG_QUALITY, 75])
    return (jpeg.tobytes(), depth_stats(depth)) if ok else None


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


def main() -> None:

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openni", required=True, help="folder with libOpenNI2.so")
    parser.add_argument("--fps", type=float, default=5.0)
    args = parser.parse_args()
    period = 1.0 / max(0.5, min(args.fps, 10.0))
    out = frame_output()
    threading.Thread(target=exit_when_stdin_closes, daemon=True).start()
    openni2.initialize(args.openni)
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
    stream.start()
    sequence, last = 0, 0.0
    try:
        while True:
            frame = stream.read_frame()
            now = time.monotonic()
            if now - last < period:
                continue
            last = now
            encoded = encode(frame)
            if encoded is None:
                continue
            jpeg, stats = encoded
            sequence += 1
            out.write(encode_frame({"sequence": sequence, **stats}, jpeg))
            out.flush()
    except BrokenPipeError:
        pass
    finally:
        stream.stop()
        openni2.unload()


if __name__ == "__main__":
    main()
