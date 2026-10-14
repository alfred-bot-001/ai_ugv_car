"""Local ROS navigation bridge inside the single Board owner.

The ugv-nav container reaches the chassis only through a 0600 UNIX socket. By default
it reads telemetry. Motion additionally needs an unexpired operator arm file and still
passes the existing hardware qualification, health, emergency-stop, lease, one-use
challenge, heartbeat and watchdog checks. First accepted on the car 2026-10-13.
"""

import contextlib
import json
import math
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .hardware import Board
from .protocol import Stick

MAX_LINEAR = 0.08  # m/s, forward only
MAX_ANGULAR = 0.3  # rad/s
CALIBRATION_MAX_LINEAR = 0.06
ARM_MAX_SECONDS = 120.0
CALIBRATION_ARM_MAX_SECONDS = 5.0
COMMAND_TIMEOUT = 0.25
TELEMETRY_MAX_AGE = 0.3
OBSERVE_SECONDS = 2.0
TRACK_WIDTH_M = 0.17452
# Low-speed skid-steer compensation for static friction; PWM limits stay unchanged.
TURN_GAIN = 2.0
PWM_LIMIT = 0.08
MAX_REQUEST = 4096


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["status"]


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["stop"]


class CmdRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["cmd"]
    linear: float = Field(ge=0, le=MAX_LINEAR, allow_inf_nan=False)
    angular: float = Field(ge=-MAX_ANGULAR, le=MAX_ANGULAR, allow_inf_nan=False)
    source: Literal["nav", "calibration"] = "nav"


AnyRequest = StatusRequest | StopRequest | CmdRequest
REQUEST: TypeAdapter[AnyRequest] = TypeAdapter(Annotated[AnyRequest, Field(discriminator="op")])


@dataclass(frozen=True)
class Arm:
    """An operator authorization read from arm.json."""

    session_id: str
    calibration: bool
    remaining: float


def read_arm(path: Path, now: float) -> Arm:
    """Reject missing, loose-permission, disabled, expired, too long or uncalibrated arms."""
    if path.stat().st_mode & 0o077:
        raise ValueError("arm.json permissions must be 0600")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("enabled") is not True:
        raise ValueError("motion is disarmed")
    expires_at = value.get("expires_at")
    if not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at):
        raise ValueError("arm expiry missing")
    remaining = float(expires_at) - now
    if not 0 < remaining <= ARM_MAX_SECONDS:
        raise ValueError("arm expired or too long")
    if value.get("units_verified") is not True:
        raise ValueError("wheel units are not verified")
    calibration = value.get("mode") == "calibration"
    # Before calibration only a few-second straight-line calibration window is allowed.
    if value.get("calibrated") is not True and not (
        calibration and remaining <= CALIBRATION_ARM_MAX_SECONDS
    ):
        raise ValueError("chassis is not calibrated")
    session_id = value.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("missing arm session id")
    return Arm(session_id, calibration, remaining)


def wheel_targets(linear: float, angular: float) -> tuple[float, float]:
    """Differential wheel speeds in m/s with the skid-steer turn compensation."""
    half = angular * TRACK_WIDTH_M * TURN_GAIN / 2
    return linear - half, linear + half


class WheelPI:
    """Close the velocity loop on encoder speed; firmware T1 is a PWM scalar on this Rover."""

    def __init__(self, kp: float = 0.25, ki: float = 0.4, windup: float = 0.05) -> None:
        self.kp, self.ki, self.windup = kp, ki, windup
        self.integral = [0.0, 0.0]

    def reset(self) -> None:
        self.integral = [0.0, 0.0]

    def step(
        self, targets: tuple[float, float], measured: tuple[float, float], dt: float
    ) -> tuple[float, float]:
        output = []
        for side in (0, 1):
            error = targets[side] - measured[side]
            self.integral[side] = max(
                -self.windup, min(self.windup, self.integral[side] + error * dt)
            )
            u = targets[side] + self.kp * error + self.ki * self.integral[side]
            # Never reverse a wheel just to correct a low-speed overshoot.
            if targets[side] >= 0:
                u = max(0.0, min(PWM_LIMIT, u))
            else:
                u = min(0.0, max(-PWM_LIMIT, u))
            output.append(u)
        return output[0], output[1]


def stick_from_wheels(left: float, right: float) -> tuple[float, float]:
    """Inverse of safety.differential at maximum 0.1, so Board.move reproduces left/right."""
    return (left - right) / 0.2, (left + right) / 0.2


class NavBridge:
    """Serves one JSON request per connection on <root>/board.sock."""

    def __init__(
        self,
        board: Board,
        root: Path,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.board = board
        self.root = root
        self.clock = clock
        self.wall = wall
        self.lease = ""
        self.sequence = 0
        self.arm_id = ""
        self.session_deadline = 0.0
        self.last_command_at = 0.0
        self.last_control_at = 0.0
        self.controller = WheelPI()
        self.threads: list[threading.Thread] = []

    @property
    def owned(self) -> bool:
        return bool(self.lease) and self.board.guard.lease == self.lease

    def stop_owned(self) -> None:
        with self.board.lock:
            if self.owned:
                self.board.stop()
            self.lease = ""

    def dispatch(self, raw: Any) -> dict[str, Any]:
        request = REQUEST.validate_python(raw)
        if isinstance(request, StatusRequest):
            return self._status()
        if isinstance(request, StopRequest):
            self.stop_owned()
            return {"ok": True}
        return self._command(request)

    def _status(self) -> dict[str, Any]:
        board = self.board
        with board.lock:
            board.observe_until = self.clock() + OBSERVE_SECONDS
            age = self.clock() - board.received_at if board.received_at else None
            return {
                "ok": True,
                "telemetry": dict(board.telemetry),
                "age": age,
                "sampled_monotonic": board.received_at,
                "estopped": board.guard.estopped,
                "nav_owned": self.owned,
            }

    def _command(self, request: CmdRequest) -> dict[str, Any]:
        if request.linear == 0 and request.angular == 0:
            self.stop_owned()
            return {"ok": True, "stopped": True}
        arm = read_arm(self.root / "arm.json", self.wall())
        if arm.calibration and (
            request.source != "calibration"
            or request.linear > CALIBRATION_MAX_LINEAR
            or request.angular != 0
        ):
            raise ValueError("calibration permits only explicit short straight tests")
        board = self.board
        with board.lock:
            board.load_evidence()
            if not board.guard.wheels_verified or board.guard.estopped or board.health_error():
                raise ValueError("existing Board qualification/health/estop rejects motion")
            now = self.clock()
            if now - board.received_at > TELEMETRY_MAX_AGE:
                raise ValueError("stale telemetry")
            if self.arm_id != arm.session_id:
                self.stop_owned()
                self.arm_id = arm.session_id
                self.session_deadline = now + min(ARM_MAX_SECONDS, arm.remaining)
            if now >= self.session_deadline:
                raise ValueError("operator session expired")
            if not self.owned:
                if self.lease:
                    # A manual stop, estop or disconnect revoked us; require a new arm id.
                    self.session_deadline = 0.0
                    raise ValueError("control revoked; re-arm required")
                self.lease = board.guard.acquire()  # rejects an existing manual lease
                self.sequence = 0
                self.controller.reset()
                self.last_control_at = now
                # The bounded operator session replaces the 2 s joystick press budget.
                # Fresh one-use challenges, heartbeat and the Board watchdog still apply.
                board.guard.press_deadline = self.session_deadline
                board.guard.lease_deadline = self.session_deadline
            self.sequence += 1
            board.guard.heartbeat()
            dt = min(0.1, max(0.0, now - self.last_control_at))
            self.last_control_at = now
            measured = (float(board.telemetry["L"]), float(board.telemetry["R"]))
            left, right = self.controller.step(
                wheel_targets(request.linear, request.angular), measured, dt
            )
            x, y = stick_from_wheels(left, right)
            board.move(
                Stick(
                    target="wheels",
                    x=x,
                    y=y,
                    lease=self.lease,
                    challenge=board.guard.challenge(),
                    sequence=self.sequence,
                )
            )
            self.last_command_at = self.clock()
        return {"ok": True}

    def watchdog_tick(self) -> None:
        """Stop when the arm lapses or the container stops streaming commands."""
        if not self.lease:
            return
        try:
            read_arm(self.root / "arm.json", self.wall())
            if self.clock() - self.last_command_at > COMMAND_TIMEOUT:
                raise ValueError("command timeout")
        except (OSError, ValueError):
            self.stop_owned()
            self.session_deadline = 0.0

    def handle(self, data: bytes) -> bytes:
        """One newline-delimited request in, one response out; any error stops owned motion."""
        try:
            response = self.dispatch(json.loads(data))
        except Exception as error:  # noqa: BLE001 - every failure must stop owned motion
            self.stop_owned()
            response = {"ok": False, "error": str(error)}
        return json.dumps(response, allow_nan=False).encode() + b"\n"

    def serve(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / "board.sock"
        path.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(4)
            server.settimeout(0.3)
            while not self.board.closed.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(0.15)
                    data = b""
                    with contextlib.suppress(OSError):
                        while b"\n" not in data and len(data) < MAX_REQUEST:
                            part = connection.recv(MAX_REQUEST - len(data))
                            if not part:
                                break
                            data += part
                    try:
                        connection.sendall(self.handle(data))
                    except OSError:
                        self.stop_owned()
        path.unlink(missing_ok=True)

    def _watchdog(self) -> None:
        while not self.board.closed.wait(0.025):
            self.watchdog_tick()

    def start(self) -> None:
        for target, name in ((self.serve, "ros-nav-socket"), (self._watchdog, "ros-nav-watchdog")):
            thread = threading.Thread(target=target, daemon=True, name=name)
            thread.start()
            self.threads.append(thread)
