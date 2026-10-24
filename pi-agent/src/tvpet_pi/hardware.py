"""Exclusive factory UART and system telemetry. Capture is never automatic."""

import contextlib
import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import serial

from .protocol import Lights, Stick
from .safety import Guard, circle, differential

# Measured wheel speed (m/s) above this is a runaway: emergency stop under a lease.
# Raised from 0.2 on 2026-10-14 for 0.10 m/s navigation with closed-loop overshoot.
# 2026-10-23, user-approved: the web joystick may now command the UGV Rover factory
# rating of 1.3 m/s, so this absolute bound is 1.6 m/s, the rating plus the ~25 %
# closed-loop overshoot measured at low speed (0.10 commanded -> 0.127 measured). The
# navigation bridge passes its own tighter bound (0.3 m/s) to `move()`, so autonomous
# runs keep the old guard; `Board.runaway` is the bound in force for the current hold.
RUNAWAY_SPEED = 1.6
# The watchdog re-sends a held wheel state this often, under the firmware's 300 ms
# T136 stop, so motion no longer depends on every web packet arriving in time.
WHEEL_RESEND = 0.1
# Gimbal knob: full deflection turns at this many degrees per second, integrated at
# GIMBAL_PERIOD inside the PAN_LIMIT / TILT_MIN..TILT_MAX command limits.
# 2026-10-22, user decision: the old 2 deg per 100 ms step and T133 SPD=10 (about
# 1 deg/s at the servo) made the camera crawl; Waveshare's own page sends SPD 0.
# 2026-10-23, user decision: the 2026-10-02 acceptance limits (+-45 / -15..45) gave
# only a 90 deg sweep, and with the pan servo's zero about 45 deg right of straight
# ahead it all lay to the right. Widen to +-170 / -30..90: the factory firmware
# (ugv_base_ros gimbal_module.h) constrains T133 to +-180 / -30..90, and 10 deg are
# kept off the pan ends so the servo never sits on the firmware boundary. Cable
# slack at the ends is checked on the car before this goes live; the zero itself
# is recalibrated with `tvpet-gimbal-center` (firmware T502).
GIMBAL_RATE = 60.0
GIMBAL_PERIOD = 0.05
GIMBAL_SPD = 0
GIMBAL_ACC = 128
PAN_LIMIT = 170.0
TILT_MIN, TILT_MAX = -30.0, 90.0


def parse_telemetry(line: bytes) -> dict[str, Any] | None:
    """Resynchronize after boot noise, reject malformed/nonfinite telemetry."""
    start = line.find(b'{"T":')
    if start < 0 or len(line) > 4096:
        return None
    try:
        value = json.loads(line[start:])
        if (
            not isinstance(value, dict)
            or value.get("T") != 1001
            or not all(isinstance(value.get(k), (float, int)) for k in ("L", "R", "v"))
        ):
            return None
        if not all(math.isfinite(value[k]) for k in ("L", "R", "v")):
            return None
        for key in ("pan", "tilt"):
            if key in value and (
                not isinstance(value[key], (int, float)) or not math.isfinite(value[key])
            ):
                return None
        return value
    except (ValueError, TypeError):
        return None


class Board:
    """One UART owner; a 50ms safety thread is independent of network and capture."""

    def __init__(self, port: str, guard: Guard, evidence_path: Path | None = None) -> None:
        self.guard = guard
        self.port = port
        self.serial: serial.Serial | None = None
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.telemetry: dict[str, Any] = {}
        self.received_at = 0.0
        self.error = ""
        self.pan = 0.0
        self.tilt = 0.0
        self.thread: threading.Thread | None = None
        # The ROS bridge asks for 20 Hz telemetry while it is observing.
        self.observe_until = 0.0
        self.evidence_path = evidence_path
        self.evidence: dict[str, Any] = {}
        self.gate_reason = "缺少有效的实机验收记录"
        # Held-state driving: the wheel ceiling of the last wheels command, when the
        # wheels were last written, when the gimbal was last integrated, and whether
        # a stop still has to be written once the held states go stale.
        self.hold_maximum = RUNAWAY_SPEED
        # Runaway bound in force: RUNAWAY_SPEED for the joystick, tighter for navigation.
        self.runaway = RUNAWAY_SPEED
        self.last_wheel_write = 0.0
        self.last_gimbal_at = 0.0
        self.last_gimbal_write = 0.0
        self.driving = False
        # Lights (T132) have no telemetry: the agent is their only writer and remembers
        # what it wrote; open() switches both off so the state is known.
        self.lights = {"base": False, "head": False}

    def load_evidence(self) -> None:
        self.guard.wheels_verified = self.guard.gimbal_verified = False
        self.evidence = {}
        self.gate_reason = "缺少有效的实机验收记录"
        if self.evidence_path is None:
            return
        try:
            if self.evidence_path.stat().st_mode & 0o077:
                return
            loaded = json.loads(self.evidence_path.read_text())
            if not isinstance(loaded, dict):
                return
            self.evidence = loaded
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            now = time.time()
            released_at = self.evidence.get("operator_released_at", 0)
            policy = self.evidence.get("release_policy", "timed")
            released = (
                policy in {"current_boot", "persistent"}
                and isinstance(released_at, (int, float))
                and math.isfinite(released_at)
                and 0 < self.evidence.get("verified_at", 0) <= released_at
                and (policy == "persistent" or released_at <= now)
            )
            valid = (
                (self.evidence.get("boot_id") == boot_id or (policy == "persistent" and released))
                and self.evidence.get("port") == self.port
                and self.evidence.get("schema") == 1
                and (self.evidence.get("expires_at", 0) > now or released)
            )
            if valid:
                self.guard.wheels_verified = (
                    self.evidence.get("wheels_verified") is True
                    and self.evidence.get("unit_profile") == "t1_scalar_limited"
                    and self.evidence.get("max_command") == 0.1
                    and 0 < self.evidence.get("firmware_timeout_ms", 1000) <= 500
                    and 0 < self.evidence.get("measured_stop_seconds", 10) <= 0.8
                )
                self.guard.gimbal_verified = self.evidence.get("gimbal_verified") is True
                self.gate_reason = (
                    "硬件已验收，检查正常后自动开放控制"
                    if self.guard.wheels_verified and self.guard.gimbal_verified
                    else "固件超时、编码器单位及云台反馈尚未完成实机验收"
                )
            elif self.evidence.get("boot_id") != boot_id:
                self.gate_reason = "Pi 已重启，需核对硬件后重新放行"
            elif self.evidence.get("expires_at", 0) <= now:
                self.gate_reason = "验收记录已过期"
        except (OSError, ValueError, TypeError):
            pass

    def open(self) -> None:
        try:
            self.serial = serial.Serial(
                self.port, 115200, timeout=0.025, write_timeout=0.1, exclusive=True
            )
            self.write({"T": 1, "L": 0.0, "R": 0.0})
            self.write({"T": 131, "cmd": 0})
            self._write_lights()
            self.load_evidence()
            if self.guard.wheels_verified:
                self.write({"T": 136, "cmd": int(self.evidence.get("firmware_timeout_ms", 300))})
            if self.guard.gimbal_verified:
                self.write({"T": 135})
            self.thread = threading.Thread(target=self._watchdog, daemon=True)
            self.thread.start()
        except (OSError, serial.SerialException) as error:
            self.error = str(error)

    def write(self, command: dict[str, Any]) -> None:
        with self.lock:
            if self.serial is None:
                raise ValueError("UART 不可用")
            self.serial.write((json.dumps(command, allow_nan=False) + "\n").encode())

    def _write_lights(self) -> None:
        self.write(
            {
                "T": 132,
                "IO4": 255 if self.lights["base"] else 0,
                "IO5": 255 if self.lights["head"] else 0,
            }
        )

    def set_lights(self, lights: Lights) -> dict[str, bool]:
        """Switch the given channels now; needs the UART, not a lease or the gate."""
        with self.lock:
            wanted = dict(self.lights)
            for name, value in lights.model_dump(exclude_none=True).items():
                wanted[name] = bool(value)
            if self.serial is None:
                raise ValueError("UART 不可用")
            self.lights = wanted
            self._write_lights()
            return dict(self.lights)

    def stop(self, *, emergency: bool = False) -> None:
        with self.lock:
            self.guard.release(emergency=emergency)
            self._settle(gimbal_stop=True)

    def _settle(self, *, gimbal_stop: bool) -> None:
        """Write the stop commands once; the lease (if any) is untouched.

        A stale hold only zeroes the wheels: the gimbal is already at (or within one
        3 deg step of) its last target, and the lifted-car probe of 2026-10-22 showed
        T135 letting the tilt servo sag about 6 deg under the camera's weight.
        """
        self.driving = False
        self.write({"T": 1, "L": 0.0, "R": 0.0})
        if gimbal_stop and self.guard.gimbal_verified:
            self.write({"T": 135})

    def move(
        self, stick: Stick, maximum: float | None = None, runaway: float = RUNAWAY_SPEED
    ) -> None:
        """Hold a stick state and drive it now; the watchdog keeps driving it (0.5 s).

        `runaway` is the measured wheel speed that stops this hold as a runaway; it is
        capped at RUNAWAY_SPEED and stays in force until the next wheels command.
        """
        if maximum is None:
            maximum = stick.speed
        with self.lock:
            self.load_evidence()
            health_error = self.health_error()
            if health_error:
                self.stop()
                raise ValueError(health_error)
            if self.guard.accept(stick) is None:
                return  # an older state arrived late; the newer one is already held
            if stick.target == "wheels":
                self.runaway = min(runaway, RUNAWAY_SPEED)
                self.hold_maximum = min(maximum, self.runaway)
                self._drive_wheels(stick)
            else:
                self._drive_gimbal(stick, time.monotonic())

    def _drive(self) -> None:
        """Watchdog tick: re-drive fresh held states, settle once when none is left."""
        now = time.monotonic()
        wheels = self.guard.holding("wheels")
        gimbal = self.guard.holding("gimbal")
        if wheels is not None and now - self.last_wheel_write >= WHEEL_RESEND:
            self._drive_wheels(wheels)
        if gimbal is not None:
            if now - self.last_gimbal_write >= GIMBAL_PERIOD:
                self._drive_gimbal(gimbal, now)
        else:
            self.last_gimbal_at = 0.0
        if self.driving and wheels is None and gimbal is None:
            self._settle(gimbal_stop=False)

    def _drive_wheels(self, stick: Stick) -> None:
        left, right = differential(stick.x, stick.y, self.hold_maximum)
        self.write({"T": 1, "L": left, "R": right})
        self.last_wheel_write = time.monotonic()
        self.driving = True

    def _drive_gimbal(self, stick: Stick, now: float) -> None:
        """Integrate the knob as a turning rate; absolute T133 targets stay in limits."""
        dt = min(0.2, now - self.last_gimbal_at) if self.last_gimbal_at else 0.0
        self.last_gimbal_at = now
        self.last_gimbal_write = now
        x, y = circle(stick.x, stick.y)
        if not dt or not (x or y):
            return
        self.pan = max(-PAN_LIMIT, min(PAN_LIMIT, self.pan + x * GIMBAL_RATE * dt))
        self.tilt = max(TILT_MIN, min(TILT_MAX, self.tilt + y * GIMBAL_RATE * dt))
        self.write(
            {
                "T": 133,
                "X": round(self.pan, 2),
                "Y": round(self.tilt, 2),
                "SPD": GIMBAL_SPD,
                "ACC": GIMBAL_ACC,
            }
        )
        self.driving = True

    def acquire(self) -> str:
        # Idle telemetry is sampled once per second. Refresh before leasing so the
        # independent 600ms stale-data watchdog does not revoke a new press.
        requested_at = time.monotonic()
        self.write({"T": 130})
        while time.monotonic() - requested_at < 0.3:
            with self.lock:
                if self.received_at >= requested_at:
                    break
            time.sleep(0.01)
        with self.lock:
            self.load_evidence()
            if not (self.guard.wheels_verified or self.guard.gimbal_verified):
                raise ValueError("设备未完成实机验收")
            if self.received_at < requested_at:
                raise ValueError("遥测已过期")
            health_error = self.health_error()
            if health_error:
                raise ValueError(health_error)
            self.pan = float(self.telemetry.get("pan", 0))
            self.tilt = float(self.telemetry.get("tilt", 0))
            self.last_gimbal_at = 0.0
            return self.guard.acquire()

    def _watchdog(self) -> None:
        last_request = 0.0
        buffer = b""
        while not self.closed.wait(0.025):
            try:
                with self.lock:
                    if self.guard.expired():
                        self.stop()
                    if self.guard.lease:
                        if any(
                            abs(float(self.telemetry.get(k, 0))) > self.runaway for k in ("L", "R")
                        ):
                            self.stop(emergency=True)
                        elif self.health_error() or time.monotonic() - self.received_at > 0.6:
                            self.stop()
                    self._drive()
                    if time.monotonic() < self.observe_until:
                        interval = 0.05
                    else:
                        interval = 0.1 if self.guard.lease else 1.0
                    if time.monotonic() - last_request >= interval:
                        # The verified car has the factory two-servo gimbal module.
                        self.write({"T": 4, "cmd": 2})
                        self.write({"T": 130})
                        last_request = time.monotonic()
                    assert self.serial is not None
                    buffer += self.serial.read(min(4096, max(1, self.serial.in_waiting)))
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        value = parse_telemetry(line)
                        if value is not None:
                            self.telemetry = value
                            self.received_at = time.monotonic()
                            self.error = ""
                    if len(buffer) > 4096:
                        buffer = b""
            except (OSError, ValueError, serial.SerialException) as error:
                self.error = str(error)
                self.guard.disconnect()

    def health_error(self) -> str:
        """Check current hardware feedback independently from saved qualification."""
        if self.serial is None or self.error:
            return "UART 连接异常"
        if not self.received_at or time.monotonic() - self.received_at > 1.5:
            return "等待新的硬件遥测"
        if not all(
            isinstance(self.telemetry.get(key), (int, float)) and math.isfinite(self.telemetry[key])
            for key in ("L", "R", "v", "pan", "tilt")
        ):
            return "硬件反馈不完整或无效"
        if self.telemetry["v"] <= 0 or any(
            abs(self.telemetry[key]) > self.runaway for key in ("L", "R")
        ):
            return "底盘速度或电压反馈异常"
        # Allow 2 deg of measured servo overshoot at the fast factory speed (2026-10-22,
        # was 0.5 deg at SPD 10) beyond the command limits.
        pan, tilt = self.telemetry["pan"], self.telemetry["tilt"]
        if not -PAN_LIMIT - 2 <= pan <= PAN_LIMIT + 2 or not TILT_MIN - 2 <= tilt <= TILT_MAX + 2:
            return "云台反馈超出控制范围"
        return ""

    def status(self) -> dict[str, Any]:
        with self.lock:
            self.load_evidence()
            health_error = self.health_error()
        age = time.monotonic() - self.received_at if self.received_at else None
        fresh = age is not None and age < 3
        return {
            "state": "available" if not health_error else "fault",
            "source": self.port,
            "age_seconds": age,
            "error": self.error,
            "telemetry": self.telemetry if fresh else {},
            "wheels_ready": self.guard.wheels_verified and not health_error,
            "gimbal_ready": self.guard.gimbal_verified and not health_error,
            "estopped": self.guard.estopped,
            "lights": dict(self.lights),
            "control_qualified": self.guard.wheels_verified and self.guard.gimbal_verified,
            "gate_reason": health_error or self.gate_reason,
        }

    def close(self) -> None:
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=1)
        with contextlib.suppress(OSError, ValueError, serial.SerialException):
            self.stop()
        if self.serial:
            self.serial.close()


def system_status() -> dict[str, Any]:
    """Collect hardware facts; missing metrics stay null rather than fabricated."""
    memory: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, rest = line.split(":", 1)
        memory[key] = int(rest.split()[0]) * 1024
    temp_path = Path("/sys/class/thermal/thermal_zone0/temp")
    temp = int(temp_path.read_text()) / 1000 if temp_path.exists() else None
    fan_paths = list(Path("/sys/class/hwmon").glob("hwmon*/fan1_input"))
    fan = int(fan_paths[0].read_text()) if fan_paths else None
    throttled: str | None = None
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        throttled = (
            subprocess.check_output(["vcgencmd", "get_throttled"], timeout=1).decode().strip()
        )
    return {
        "hostname": os.uname().nodename,
        "temperature_c": temp,
        "memory_total": memory["MemTotal"],
        "memory_used": memory["MemTotal"] - memory["MemAvailable"],
        "load1": os.getloadavg()[0],
        "fan_rpm": fan,
        "throttled": throttled,
        "sampled_at": time.time(),
        "source": "linux/sysfs/proc",
    }
