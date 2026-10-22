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

# 2026-10-14, user-approved: Nav2 now runs at 0.10 m/s and 0.5 rad/s (was 0.06 / 0.25).
MAX_LINEAR = 0.12  # m/s, forward only
MAX_ANGULAR = 0.6  # rad/s
CALIBRATION_MAX_LINEAR = 0.06
ARM_MAX_SECONDS = 120.0
CALIBRATION_ARM_MAX_SECONDS = 5.0
COMMAND_TIMEOUT = 0.25
TELEMETRY_MAX_AGE = 0.3
OBSERVE_SECONDS = 2.0
# 2026-10-21, user-approved (plan A, the factory way): T1 L/R are m/s targets for the
# firmware's own speed PID, exactly as T13 is (both call setGoalSpeed). The bridge no
# longer runs its own PI on top or doubles turns; wheel targets use the firmware's
# T13 formula. Probes showed the firmware stalls and stutters below about 0.06 m/s
# per wheel (0.064 m/s speed steps plus a 23/255 PWM dead zone), so each side is
# either still or at least WHEEL_FLOOR.
WHEEL_FLOOR = 0.07  # m/s
# T1 wheel-speed ceiling for navigation (m/s); stays below the Board runaway threshold.
PWM_LIMIT = 0.18
MAX_REQUEST = 4096
# S0 single-wheel stall (docs/task-autonomy-design.md §3.1 A). Initial values, to be
# calibrated on the car so normal starts, spins and carpet edges never trip them.
# Targets are 0 or >= WHEEL_FLOOR, where the firmware turns the wheel without pauses.
STALL_TARGET_MIN = 0.06  # m/s commanded on the wheel
STALL_MEASURED_MAX = 0.01  # m/s measured: the wheel is not turning
STALL_WHEEL_SECONDS = 1.5
# Motion probe (2026-10-21): bounded factory-command steps to compare the firmware's own
# T13 / T1 speed loop with this bridge's T1 + PI, with per-side encoder traces.
PROBE_ARM_MAX_SECONDS = 10.0
PROBE_MAX_SECONDS = 2.0
PROBE_MAX_LINEAR = 0.10  # m/s for T13 X
PROBE_MAX_ANGULAR = 0.6  # rad/s for T13 Z
PROBE_MAX_WHEEL = 0.10  # T1 L/R magnitude (m/s on current factory firmware)
PROBE_PERIOD = 0.05
FIRMWARE_TRACK_WIDTH_M = 0.172  # what the factory firmware uses inside T13


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["status"]


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["stop"]
    # A container-side safety check; the current arm session can no longer move.
    reason: Literal["stall_body", "localization_lost"] | None = None


class CmdRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["cmd"]
    linear: float = Field(ge=0, le=MAX_LINEAR, allow_inf_nan=False)
    angular: float = Field(ge=-MAX_ANGULAR, le=MAX_ANGULAR, allow_inf_nan=False)
    source: Literal["nav", "calibration"] = "nav"


class ProbeRequest(BaseModel):
    """One factory motion command held for a few seconds; needs a probe-mode arm."""

    model_config = ConfigDict(extra="forbid")
    op: Literal["probe"]
    command: Literal["t13", "t1"]
    a: float = Field(allow_inf_nan=False)  # T13 X (m/s) or T1 L
    b: float = Field(allow_inf_nan=False)  # T13 Z (rad/s) or T1 R
    seconds: float = Field(gt=0, le=PROBE_MAX_SECONDS, allow_inf_nan=False)

    def frame(self) -> dict[str, Any]:
        if self.command == "t13":
            if abs(self.a) > PROBE_MAX_LINEAR or abs(self.b) > PROBE_MAX_ANGULAR:
                raise ValueError("probe T13 limits: |X| <= 0.10 m/s, |Z| <= 0.6 rad/s")
            return {"T": 13, "X": self.a, "Z": self.b}
        if abs(self.a) > PROBE_MAX_WHEEL or abs(self.b) > PROBE_MAX_WHEEL:
            raise ValueError("probe T1 limits: |L|, |R| <= 0.10")
        return {"T": 1, "L": self.a, "R": self.b}

    def targets(self) -> tuple[float, float]:
        """Per-side speeds the factory firmware aims for (T1 L/R taken as m/s)."""
        if self.command == "t13":
            half = self.b * FIRMWARE_TRACK_WIDTH_M / 2
            return self.a - half, self.a + half
        return self.a, self.b


AnyRequest = StatusRequest | StopRequest | CmdRequest | ProbeRequest
REQUEST: TypeAdapter[AnyRequest] = TypeAdapter(Annotated[AnyRequest, Field(discriminator="op")])


@dataclass(frozen=True)
class Arm:
    """An operator authorization read from arm.json."""

    session_id: str
    calibration: bool
    remaining: float
    probe: bool = False


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
    probe = value.get("mode") == "probe"
    if probe and remaining > PROBE_ARM_MAX_SECONDS:
        raise ValueError("probe arm too long")
    # Before calibration only a few-second straight-line calibration window is allowed.
    if value.get("calibrated") is not True and not (
        calibration and remaining <= CALIBRATION_ARM_MAX_SECONDS
    ):
        raise ValueError("chassis is not calibrated")
    session_id = value.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("missing arm session id")
    return Arm(session_id, calibration, remaining, probe)


def floor_wheels(left: float, right: float, floor: float = WHEEL_FLOOR) -> tuple[float, float]:
    """Keep each side still or at least `floor` m/s, where the firmware PID runs smoothly.

    Turning in place (opposite signs): both sides lifted to the floor. Same direction:
    the curvature is kept while the outer side is lifted to the floor; an inner side
    below half the floor stops (pivot), otherwise it is lifted to the floor.
    """
    if left == 0 and right == 0:
        return 0.0, 0.0
    if left * right < 0:
        return (
            math.copysign(max(abs(left), floor), left),
            math.copysign(max(abs(right), floor), right),
        )
    outer = max(abs(left), abs(right))
    if outer < floor:
        left, right = left * floor / outer, right * floor / outer

    def side(value: float) -> float:
        if abs(value) < floor / 2:
            return 0.0
        return math.copysign(max(abs(value), floor), value)

    return side(left), side(right)


def wheel_targets(linear: float, angular: float) -> tuple[float, float]:
    """Wheel speeds in m/s by the firmware's T13 formula, floored and capped."""
    half = angular * FIRMWARE_TRACK_WIDTH_M / 2
    left, right = floor_wheels(linear - half, linear + half)
    return (
        max(-PWM_LIMIT, min(PWM_LIMIT, left)),
        max(-PWM_LIMIT, min(PWM_LIMIT, right)),
    )


class StallMonitor:
    """A wheel commanded at a speed the firmware turns smoothly that still does not turn."""

    def __init__(self, seconds: float = STALL_WHEEL_SECONDS) -> None:
        self.seconds = seconds
        self.since: list[float | None] = [None, None]

    def reset(self) -> None:
        self.since = [None, None]

    def update(
        self,
        now: float,
        targets: tuple[float, float],
        measured: tuple[float, float],
    ) -> str | None:
        """Return "left" or "right" once that wheel has been stalled for `seconds`."""
        for side, name in ((0, "left"), (1, "right")):
            stalled = (
                abs(targets[side]) >= STALL_TARGET_MIN and abs(measured[side]) < STALL_MEASURED_MAX
            )
            if not stalled:
                self.since[side] = None
                continue
            started = self.since[side]
            if started is None:
                self.since[side] = started = now
            if now - started >= self.seconds:
                return name
        return None


def stick_from_wheels(left: float, right: float, maximum: float = PWM_LIMIT) -> tuple[float, float]:
    """Inverse of safety.differential at `maximum`, so Board.move reproduces left/right."""
    return (left - right) / (2 * maximum), (left + right) / (2 * maximum)


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
        self.stall_monitor = StallMonitor()
        # Set by a safety stop; cleared only by a new operator arm session.
        self.fault: dict[str, Any] | None = None
        self.threads: list[threading.Thread] = []
        # Told about safety stops (the map page log); must not raise.
        self.notify: Callable[[str], None] | None = None

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
        if isinstance(request, ProbeRequest):
            return self._probe(request)
        if isinstance(request, StopRequest):
            self.stop_owned()
            if request.reason:
                self.halt(request.reason)
            return {"ok": True}
        return self._command(request)

    def halt(self, reason: str) -> None:
        """Record a safety stop; this arm session can no longer move the car."""
        self.fault = {"reason": reason, "at": self.wall()}
        self.session_deadline = 0.0
        if self.notify is not None:
            with contextlib.suppress(Exception):
                self.notify(reason)

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
                "fault": self.fault,
            }

    def _command(self, request: CmdRequest) -> dict[str, Any]:
        if request.linear == 0 and request.angular == 0:
            self.stop_owned()
            return {"ok": True, "stopped": True}
        arm = read_arm(self.root / "arm.json", self.wall())
        if arm.probe:
            raise ValueError("a probe arm only allows probe requests")
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
                self.fault = None
            if self.fault:
                raise ValueError(f"halted ({self.fault['reason']}); re-arm required")
            if now >= self.session_deadline:
                raise ValueError("operator session expired")
            if not self.owned:
                if self.lease:
                    # A manual stop, estop or disconnect revoked us; require a new arm id.
                    self.session_deadline = 0.0
                    raise ValueError("control revoked; re-arm required")
                self.lease = board.guard.acquire()  # rejects an existing manual lease
                self.sequence = 0
                self.stall_monitor.reset()
                # The bounded operator session is the only time budget; the heartbeat
                # and the Board watchdog still apply.
                board.guard.press_deadline = self.session_deadline
                board.guard.lease_deadline = self.session_deadline
            self.sequence += 1
            board.guard.heartbeat()
            measured = (float(board.telemetry["L"]), float(board.telemetry["R"]))
            left, right = wheel_targets(request.linear, request.angular)
            stalled = self.stall_monitor.update(now, (left, right), measured)
            if stalled:
                self.halt(f"stall_wheel_{stalled}")
                raise ValueError(f"stall: {stalled} wheel commanded but not turning")
            x, y = stick_from_wheels(left, right)
            board.move(
                maximum=PWM_LIMIT,
                stick=Stick(
                    target="wheels",
                    x=x,
                    y=y,
                    lease=self.lease,
                    sequence=self.sequence,
                ),
            )
            self.last_command_at = self.clock()
        return {"ok": True}

    def _probe(self, request: ProbeRequest) -> dict[str, Any]:
        """Hold one factory command for request.seconds and trace both encoders.

        Same gates as navigation (qualification, health, estop, lease, fresh telemetry);
        the Board watchdog keeps its runaway and stale-telemetry stops because the lock
        is only held per step. Always ends with the stop command.
        """
        frame = request.frame()
        arm = read_arm(self.root / "arm.json", self.wall())
        if not arm.probe:
            raise ValueError("probe needs a probe-mode arm")
        board = self.board
        with board.lock:
            board.load_evidence()
            if not board.guard.wheels_verified or board.guard.estopped or board.health_error():
                raise ValueError("existing Board qualification/health/estop rejects motion")
            if self.owned or self.lease:
                raise ValueError("navigation already owns the chassis")
            started = self.clock()
            if started - board.received_at > TELEMETRY_MAX_AGE:
                raise ValueError("stale telemetry")
            self.arm_id = arm.session_id
            self.session_deadline = started + min(arm.remaining, request.seconds + 1)
            self.lease = board.guard.acquire()
            board.guard.press_deadline = board.guard.lease_deadline = self.session_deadline
            board.observe_until = started + request.seconds + 1
            start_odl = board.telemetry.get("odl")
            start_odr = board.telemetry.get("odr")
        samples: list[dict[str, float]] = []
        last_seen = 0.0
        reason = ""
        try:
            while self.clock() - started < request.seconds:
                with board.lock:
                    if not self.owned or board.guard.estopped:
                        reason = "lease revoked or emergency stop"
                        break
                    if board.health_error():
                        reason = board.health_error()
                        break
                    board.guard.heartbeat()
                    board.write(frame)
                    self.last_command_at = self.clock()
                    if board.received_at != last_seen:
                        last_seen = board.received_at
                        samples.append(
                            {
                                "t": round(board.received_at - started, 3),
                                "L": float(board.telemetry["L"]),
                                "R": float(board.telemetry["R"]),
                            }
                        )
                time.sleep(PROBE_PERIOD)
        finally:
            self.stop_owned()
            self.session_deadline = 0.0
        with board.lock:
            end = {"odl": board.telemetry.get("odl"), "odr": board.telemetry.get("odr")}
        targets = request.targets()
        return {
            "ok": not reason,
            "error": reason or None,
            "frame": frame,
            "targets": {"L": targets[0], "R": targets[1]},
            "samples": samples,
            "odometry": {"start": {"odl": start_odl, "odr": start_odr}, "end": end},
        }

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
