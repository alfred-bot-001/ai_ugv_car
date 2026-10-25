"""Front Orbbec depth: frames from an OpenNI2 worker process, leased like the cameras.

The depth sensor (USB 2bc5:0614) needs OpenNI2 with Orbbec's driver, numpy and OpenCV,
which live in pose-venv, so tvpet_pi.depth_worker runs there as a child process and
writes colorized JPEG frames to stdout. This module reads them for CaptureWorker; the
worker exits when its stdin closes, so it never outlives the agent.
"""

import json
import struct
import subprocess
import time
from dataclasses import dataclass, field
from typing import IO, Any

from .capture import Frame
from .protocol import MAX_JPEG

MAX_META = 4096
HEADER = struct.Struct("!II")  # metadata length, JPEG length
DEPTH_FPS = 5
# Colour scale of the depth image (mm): blue near, through green and yellow, red far.
NEAR_MM = 300
FAR_MM = 4000


@dataclass(frozen=True)
class DepthFrame(Frame):
    """A colorized depth JPEG plus what the page shows about it (distances in mm)."""

    meta: dict[str, Any] = field(default_factory=dict)


def encode_frame(meta: dict[str, Any], jpeg: bytes) -> bytes:
    data = json.dumps(meta, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > MAX_META or not jpeg.startswith(b"\xff\xd8") or len(jpeg) > MAX_JPEG:
        raise ValueError("invalid depth frame")
    return HEADER.pack(len(data), len(jpeg)) + data + jpeg


def read_exact(stream: IO[bytes], size: int) -> bytes:
    data = b""
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            raise EOFError("depth worker closed its output")
        data += part
    return data


def read_frame(stream: IO[bytes]) -> tuple[dict[str, Any], bytes]:
    """One frame from the worker; malformed output raises ValueError."""
    meta_size, jpeg_size = HEADER.unpack(read_exact(stream, HEADER.size))
    if not 1 <= meta_size <= MAX_META or not 2 <= jpeg_size <= MAX_JPEG:
        raise ValueError("invalid depth frame header")
    meta = json.loads(read_exact(stream, meta_size))
    jpeg = read_exact(stream, jpeg_size)
    if not isinstance(meta, dict) or not isinstance(meta.get("sequence"), int):
        raise ValueError("invalid depth frame metadata")
    if not jpeg.startswith(b"\xff\xd8"):
        raise ValueError("invalid depth JPEG")
    return meta, jpeg


class DepthCamera:
    """CaptureWorker adapter: open starts the worker, read returns its next frame."""

    def __init__(self, python: str, openni: str, command: list[str] | None = None) -> None:
        self.python = python
        self.openni = openni
        self.command = command or [
            python,
            "-m",
            "tvpet_pi.depth_worker",
            "--openni",
            openni,
            "--fps",
            str(DEPTH_FPS),
        ]
        self.process: subprocess.Popen[bytes] | None = None

    def open(self) -> None:
        self.close()
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def read(self) -> DepthFrame:
        process = self.process
        if process is None or process.stdout is None:
            raise OSError("depth worker is not running")
        try:
            meta, jpeg = read_frame(process.stdout)
        except EOFError as error:
            code = process.poll()
            raise OSError(f"depth worker stopped (exit {code})") from error
        return DepthFrame(int(meta["sequence"]), time.monotonic_ns(), 0, jpeg, meta)

    def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.stdin:
            process.stdin.close()  # the worker exits when its stdin closes
        try:
            process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.5)
        if process.stdout:
            process.stdout.close()
