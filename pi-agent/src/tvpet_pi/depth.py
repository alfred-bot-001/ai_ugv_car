"""Front Orbbec depth: frames from an OpenNI2 worker process, leased like the cameras.

The depth sensor (USB 2bc5:0614) needs OpenNI2 with Orbbec's driver, numpy and OpenCV,
which live in pose-venv, so tvpet_pi.depth_worker runs there as a child process and
writes colorized JPEG frames to stdout (tvpet_pi.depth_wire framing). This module reads
them for CaptureWorker; the worker exits when its stdin closes, so it never outlives the
agent. Its stderr goes to the agent's journal, so a worker that cannot start says why.
"""

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from .capture import Frame
from .depth_wire import read_frame

DEPTH_FPS = 5
# One thread for numpy's BLAS and OpenMP in the worker: their idle threads spin between
# calls, which took the worker above a whole core of the Pi 5 (2026-10-25).
WORKER_ENV = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
# Floor calibration for the Nav2 scan (tvpet_pi.depth_worker --calibrate writes it).
DEFAULT_FLOOR = "~/workspace-agent/agent-data/ai-ugv-car/pi-agent/depth-floor.json"


@dataclass(frozen=True)
class DepthFrame(Frame):
    """A colorized depth JPEG plus what the page shows about it (distances in mm)."""

    meta: dict[str, Any] = field(default_factory=dict)


class DepthCamera:
    """CaptureWorker adapter: open starts the worker, read returns its next frame."""

    def __init__(
        self, python: str, openni: str, floor: str = "", command: list[str] | None = None
    ) -> None:
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
            *(["--floor", floor] if floor else []),
        ]
        self.process: subprocess.Popen[bytes] | None = None

    def open(self) -> None:
        self.close()
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env={**os.environ, **WORKER_ENV},
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
