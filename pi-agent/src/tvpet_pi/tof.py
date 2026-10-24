"""SEN0628 8x8 ToF matrix through the XIAO USB bridge: frames, health, floor baseline, stops.

The D500 lidar scans 0.16 m above the floor, so door sills and cushions below that
plane were only noticed by the stall checks after the car had already pushed into
them (docs/task-autonomy-design.md S0-1). The ToF matrix sits at the front edge,
8-12 cm up and pitched 15-20 deg down: its upper rows look ahead, the lower rows at
the floor 8-30 cm in front. `firmware/tof-bridge/` streams the 64 distances as text
lines; this module parses them, tracks sensor health, compares each cell with a
floor baseline recorded on level ground and asks the Board for an ordinary stop,
the same path as a lost link. Nothing here ever starts motion.
"""

import contextlib
import json
import os
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import serial

CELLS = 64
COLUMNS = 8
INVALID_MM = 4000  # the sensor's "no valid return" (firmware V1.3)
MAX_MM = 65535  # the bridge prints uint16
FRAME_FIELDS = 3 + CELLS + 1  # tag, seq, millis, 64 cells, sum16
# No frame for this long and the sensor counts as unhealthy.
FRAME_TIMEOUT = 1.0
# Opening the port resets the XIAO; the bench showed the first frame about 10 s later
# (boot, 8x8 mode set, the sensor's 5 s settle, a few retried reads).
STARTUP_GRACE = 15.0
REOPEN_INTERVAL = 5.0
BASELINE_RECHECK = 2.0
RATE_WINDOW = 2.0
DEFAULT_BASELINE = "~/workspace-agent/agent-data/ai-ugv-car/pi-agent/tof-baseline.json"
BASELINE_SCHEMA = 1

# Detection thresholds, 2026-10-24 initial values; S0-1 / S0-2 on the car calibrate
# them (sill and cushion must stop within 3 s, starts, spins and carpet edges never).
# A baseline cell nearer than this saw the floor at calibration time.
GROUND_MAX_MM = 600
# A floor cell reading this fraction of its baseline, and at least the drop, is
# something standing on the floor. A 25 mm sill 20 cm ahead shortens the ray ~50 mm.
OBSTACLE_RATIO = 0.8
OBSTACLE_MIN_DROP_MM = 40
# A floor cell reading much further than its baseline, or no return at all, has lost
# the floor: a step down or an edge. Dark rugs can also swallow the return, so this
# needs more agreeing cells and can be switched off in the agent config.
CLIFF_RATIO = 1.3
CLIFF_MIN_RISE_MM = 60
CLIFF_MIN_CELLS = 4
# Cells that did not see the floor at calibration look ahead: in the centre columns
# anything nearer than this is an obstacle. Nav2's own gate stops at 0.30 m.
FRONT_STOP_MM = 250
FRONT_COLUMNS = range(2, 6)
# Cells that must agree within a frame, and frames in a row (about 200 ms at 15 Hz).
OBSTACLE_MIN_CELLS = 3
CONSECUTIVE_FRAMES = 3

REASON_LOW_OBSTACLE = "tof_low_obstacle"
REASON_CLIFF = "tof_cliff"
REASON_TEXT = {REASON_LOW_OBSTACLE: "tof: low obstacle", REASON_CLIFF: "tof: cliff"}


@dataclass(frozen=True)
class Frame:
    tag: str  # "T" changed matrix, "H" heartbeat
    seq: int
    millis: int
    values: tuple[int, ...]


def parse_frame(line: bytes) -> Frame | None:
    """One bridge line to a Frame; status lines, boot noise and bad checksums give None."""
    if len(line) > 1024:
        return None
    text = line.strip().decode("ascii", "replace")
    if not text or text[0] not in "TH":
        return None
    fields = text.split(",")
    if len(fields) != FRAME_FIELDS or not all(f.isdigit() for f in fields[1:]):
        return None
    numbers = [int(f) for f in fields[1:]]
    seq, millis, values, checksum = numbers[0], numbers[1], numbers[2:-1], numbers[-1]
    if any(v > MAX_MM for v in values) or sum(values) & 0xFFFF != checksum:
        return None
    return Frame(fields[0], seq, millis, tuple(values))


def median_baseline(frames: list[tuple[int, ...]]) -> tuple[int, ...]:
    """Per-cell median of several still frames; needs at least three."""
    if len(frames) < 3 or any(len(f) != CELLS for f in frames):
        raise ValueError("a baseline needs at least three 64-cell frames")
    return tuple(int(statistics.median(f[i] for f in frames)) for i in range(CELLS))


@dataclass(frozen=True)
class Baseline:
    values: tuple[int, ...]
    created_at: float
    frames: int

    def ground(self) -> tuple[bool, ...]:
        """Cells that saw the floor when the baseline was taken."""
        return tuple(0 < v < GROUND_MAX_MM for v in self.values)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": BASELINE_SCHEMA,
            "created_at": self.created_at,
            "frames": self.frames,
            "values": list(self.values),
        }

    @classmethod
    def from_json(cls, data: Any) -> "Baseline":
        if not isinstance(data, dict) or data.get("schema") != BASELINE_SCHEMA:
            raise ValueError("unknown ToF baseline schema")
        values = data.get("values")
        if (
            not isinstance(values, list)
            or len(values) != CELLS
            or not all(isinstance(v, int) and 0 <= v <= MAX_MM for v in values)
        ):
            raise ValueError("ToF baseline needs 64 integer cells")
        created_at = data.get("created_at", 0)
        frames = data.get("frames", 0)
        if not isinstance(created_at, (int, float)) or not isinstance(frames, int):
            raise ValueError("ToF baseline metadata invalid")
        return cls(tuple(values), float(created_at), frames)

    def save(self, path: Path) -> None:
        """Runtime data: directory 0700, file 0600, written atomically."""
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
            json.dump(self.to_json(), out)
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Baseline":
        return cls.from_json(json.loads(path.read_text()))


def classify(
    values: tuple[int, ...], baseline: Baseline, *, cliffs: bool = True
) -> tuple[list[int], list[int]]:
    """Cells of one frame that look like a low obstacle, and cells that lost the floor.

    4000 is never "far": on a floor cell it is missing floor (cliff evidence), on a
    forward-looking cell it is unknown and counts for nothing.
    """
    obstacles: list[int] = []
    lost: list[int] = []
    for index, (value, reference, floor) in enumerate(
        zip(values, baseline.values, baseline.ground(), strict=True)
    ):
        if floor:
            if value >= INVALID_MM or (
                value > reference * CLIFF_RATIO and value - reference >= CLIFF_MIN_RISE_MM
            ):
                if cliffs:
                    lost.append(index)
            elif value < reference * OBSTACLE_RATIO and reference - value >= OBSTACLE_MIN_DROP_MM:
                obstacles.append(index)
        elif value < FRONT_STOP_MM and index % COLUMNS in FRONT_COLUMNS:
            obstacles.append(index)
    return obstacles, lost


class Detector:
    """Debounces classify() over consecutive frames into one stop reason or none."""

    def __init__(self, baseline: Baseline | None = None, *, cliffs: bool = True) -> None:
        self.baseline = baseline
        self.cliffs = cliffs
        self.obstacle_frames = 0
        self.cliff_frames = 0
        self.obstacle_cells: list[int] = []
        self.cliff_cells: list[int] = []
        self.reason = ""

    def reset(self) -> None:
        self.obstacle_frames = self.cliff_frames = 0
        self.obstacle_cells, self.cliff_cells = [], []
        self.reason = ""

    def update(self, values: tuple[int, ...]) -> str:
        """Feed one frame; returns the reason in force after it ("" when clear)."""
        if self.baseline is None:
            self.reset()
            return ""
        self.obstacle_cells, self.cliff_cells = classify(values, self.baseline, cliffs=self.cliffs)
        obstacle = len(self.obstacle_cells) >= OBSTACLE_MIN_CELLS
        cliff = len(self.cliff_cells) >= CLIFF_MIN_CELLS
        self.obstacle_frames = self.obstacle_frames + 1 if obstacle else 0
        self.cliff_frames = self.cliff_frames + 1 if cliff else 0
        if self.obstacle_frames >= CONSECUTIVE_FRAMES:
            self.reason = REASON_LOW_OBSTACLE
        elif self.cliff_frames >= CONSECUTIVE_FRAMES:
            self.reason = REASON_CLIFF
        else:
            self.reason = ""
        return self.reason


class RateMeter:
    def __init__(self, window: float = RATE_WINDOW) -> None:
        self.window = window
        self.stamps: deque[float] = deque()

    def add(self, now: float) -> None:
        self.stamps.append(now)
        self.trim(now)

    def trim(self, now: float) -> None:
        while self.stamps and now - self.stamps[0] > self.window:
            self.stamps.popleft()

    def hz(self, now: float) -> float:
        self.trim(now)
        if len(self.stamps) < 2:
            return 0.0
        span = self.stamps[-1] - self.stamps[0]
        return (len(self.stamps) - 1) / span if span > 0 else 0.0


class ToFLink:
    """Sole owner of the bridge's serial port; parsing and judgement are testable via feed()."""

    def __init__(
        self,
        port: str,
        baseline_path: Path | None = None,
        *,
        cliffs: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.port = port
        self.baseline_path = baseline_path
        self.clock = clock
        self.serial: serial.Serial | None = None
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.thread: threading.Thread | None = None
        self.buffer = b""
        self.frame: Frame | None = None
        self.received_at = 0.0
        self.opened_at = 0.0
        self.error = ""
        self.bad_lines = 0
        self.frames = 0
        self.rate = RateMeter()
        self.detector = Detector(None, cliffs=cliffs)
        self.baseline_error = ""
        self.baseline_stamp: float | None = None
        self.baseline_checked = 0.0
        # Frames to hand to a one-off collector (calibration) while it is set.
        self.collector: list[tuple[int, ...]] | None = None

    # -- baseline -----------------------------------------------------------------

    def reload_baseline(self) -> None:
        """Pick up a baseline written by `tvpet-tof calibrate`; a bad file disables detection."""
        if self.baseline_path is None:
            return
        try:
            stamp = self.baseline_path.stat().st_mtime
        except OSError:
            stamp = None
        if stamp == self.baseline_stamp and (stamp is not None or self.baseline_error):
            return
        self.baseline_stamp = stamp
        baseline: Baseline | None = None
        self.baseline_error = ""
        if stamp is not None:
            try:
                baseline = Baseline.load(self.baseline_path)
            except (OSError, ValueError) as error:
                self.baseline_error = str(error)
        else:
            self.baseline_error = "no baseline recorded"
        with self.lock:
            self.detector.baseline = baseline
            self.detector.reset()

    # -- frames -------------------------------------------------------------------

    def feed(self, chunk: bytes, now: float) -> list[Frame]:
        """Split bridge bytes into lines; parse, count and judge each frame."""
        self.buffer += chunk
        frames: list[Frame] = []
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if not line.strip() or line.lstrip().startswith(b"#"):
                continue
            frame = parse_frame(line)
            if frame is None:
                self.bad_lines += 1
                continue
            frames.append(frame)
            with self.lock:
                self.frame = frame
                self.received_at = now
                self.frames += 1
                self.rate.add(now)
                self.detector.update(frame.values)
                if self.collector is not None:
                    self.collector.append(frame.values)
        if len(self.buffer) > 4096:
            self.buffer = b""
            self.bad_lines += 1
        return frames

    def state(self, now: float | None = None) -> str:
        """disabled / starting / active / fault, from the port and the last frame."""
        now = self.clock() if now is None else now
        if not self.port:
            return "disabled"
        if self.received_at and now - self.received_at <= FRAME_TIMEOUT:
            return "active"
        if self.serial is not None and not self.error and now - self.opened_at < STARTUP_GRACE:
            return "starting"
        return "fault"

    def stop_reason(self) -> str:
        """The Board's extra stop check: a reason only from a healthy sensor with a baseline."""
        with self.lock:
            if self.state() != "active" or self.detector.baseline is None:
                return ""
            return self.detector.reason

    def status(self) -> dict[str, Any]:
        now = self.clock()
        with self.lock:
            frame = self.frame
            detector = self.detector
            state = self.state(now)
            return {
                "state": state,
                "port": self.port,
                "error": self.error
                if state == "fault" and self.error
                else "等待传感器数据帧"
                if state == "fault"
                else "",
                "age_seconds": now - self.received_at if self.received_at else None,
                "hz": round(self.rate.hz(now), 1),
                "frames": self.frames,
                "bad_lines": self.bad_lines,
                "seq": frame.seq if frame else None,
                "values": list(frame.values) if frame else None,
                "baseline": detector.baseline is not None,
                "baseline_error": self.baseline_error,
                "ground_cells": sum(detector.baseline.ground()) if detector.baseline else 0,
                "obstacle_cells": list(detector.obstacle_cells),
                "cliff_cells": list(detector.cliff_cells),
                "reason": detector.reason if state == "active" else "",
                "reason_text": REASON_TEXT.get(detector.reason, "") if state == "active" else "",
            }

    # -- serial I/O ---------------------------------------------------------------

    def _open_serial(self) -> None:
        link = serial.Serial(None, 115200, timeout=0.05, exclusive=True)
        # Keep DTR/RTS low: driven high they hold the XIAO in reset or download mode.
        link.dtr = False
        link.rts = False
        link.port = self.port
        link.open()
        self.serial = link
        self.opened_at = self.clock()
        self.buffer = b""
        self.error = ""

    def _run(self) -> None:
        while not self.closed.is_set():
            link = self.serial
            if link is None:
                try:
                    self._open_serial()
                except (OSError, ValueError, serial.SerialException) as error:
                    self.error = str(error)
                    self.closed.wait(REOPEN_INTERVAL)
                continue
            try:
                chunk = link.read(max(1, min(4096, link.in_waiting)))
                if chunk:
                    self.feed(chunk, self.clock())
            except (OSError, ValueError, serial.SerialException) as error:
                self.error = str(error)
                with contextlib.suppress(Exception):
                    link.close()
                self.serial = None
                self.closed.wait(REOPEN_INTERVAL)
                continue
            if self.clock() - self.baseline_checked >= BASELINE_RECHECK:
                self.baseline_checked = self.clock()
                self.reload_baseline()

    def open(self) -> None:
        self.reload_baseline()
        self.thread = threading.Thread(target=self._run, daemon=True, name="tof-bridge")
        self.thread.start()

    def collect(self, count: int, timeout: float) -> list[tuple[int, ...]]:
        """Gather `count` consecutive frames (for a baseline); raises on timeout."""
        with self.lock:
            self.collector = []
        deadline = self.clock() + timeout
        try:
            while self.clock() < deadline:
                with self.lock:
                    if len(self.collector or []) >= count:
                        return list(self.collector or [])[:count]
                time.sleep(0.02)
            raise TimeoutError(f"only {len(self.collector or [])}/{count} ToF frames in {timeout}s")
        finally:
            with self.lock:
                self.collector = None

    def close(self) -> None:
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=1)
        if self.serial:
            with contextlib.suppress(Exception):
                self.serial.close()
            self.serial = None
