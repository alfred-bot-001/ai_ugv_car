"""Make the gimbal's current pose its zero: firmware T502 (`setMiddlePos`).

Run on the Pi with the agent stopped, because the agent owns the UART. Point the
camera straight ahead with the web knob first, stop `ai-tv-pet-agent.service`, run
`tvpet-gimbal-center --camera-facing-front`, then start the service again. The
firmware calls `st.CalibrationOfs(id)`, which stores the offset in the servo's own
EEPROM, so the new zero survives power cycles. 2026-10-23: the pan servo's zero sat
about 45 deg right of straight ahead, so the +-45 limits only ever turned right.
"""

import argparse
import json
import math
import time
from typing import Any

import serial

from .hardware import parse_telemetry

# ugv_config.h: GIMBAL_PAN_ID 2, GIMBAL_TILT_ID 1.
GIMBAL_SERVO_IDS = {"pan": 2, "tilt": 1}
CMD_SET_MIDDLE = 502
# Feedback after calibration is 0 plus the servo's 12-bit rounding and overshoot.
CENTER_TOLERANCE = 2.0


def center_command(axis: str) -> dict[str, Any]:
    return {"T": CMD_SET_MIDDLE, "id": GIMBAL_SERVO_IDS[axis]}


def _angle(axis: str, telemetry: dict[str, Any]) -> float:
    value = telemetry.get(axis)
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"telemetry has no valid {axis} feedback")
    return float(value)


def check_centered(axis: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Pure summary of one calibration: feedback before/after and whether it is at 0."""
    first, last = _angle(axis, before), _angle(axis, after)
    return {
        "axis": axis,
        "servo_id": GIMBAL_SERVO_IDS[axis],
        "before": first,
        "after": last,
        "centered": abs(last) <= CENTER_TOLERANCE,
    }


def center(port: str, axis: str, *, settle: float = 1.0, window: float = 0.5) -> dict[str, Any]:
    """Zero the wheels, enable gimbal feedback, send T502 for the axis, re-read."""
    with serial.Serial(port, 115200, timeout=0.02, write_timeout=0.1, exclusive=True) as uart:

        def send(command: dict[str, Any]) -> None:
            uart.write((json.dumps(command) + "\n").encode())

        def latest(duration: float) -> dict[str, Any]:
            started, last_request, value = time.monotonic(), 0.0, None
            while True:
                if time.monotonic() - last_request > 0.08:
                    send({"T": 130})
                    last_request = time.monotonic()
                parsed = parse_telemetry(uart.readline(4096))
                if parsed is not None:
                    value = parsed
                if time.monotonic() - started >= duration:
                    break
            if value is None or axis not in value:
                raise RuntimeError(f"No fresh gimbal {axis} telemetry")
            return value

        send({"T": 1, "L": 0.0, "R": 0.0})
        send({"T": 4, "cmd": 2})
        time.sleep(settle)
        before = latest(window)
        send(center_command(axis))
        time.sleep(settle)
        after = latest(window)
        result = check_centered(axis, before, after)
        result["port"] = port
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="把云台当前位置设为舵机零位（固件 T502）")
    parser.add_argument(
        "--camera-facing-front",
        action="store_true",
        required=True,
        help="操作者确认相机此刻正对车头（先停掉 ai-tv-pet-agent 再运行）",
    )
    parser.add_argument("--axis", choices=sorted(GIMBAL_SERVO_IDS), default="pan")
    parser.add_argument("--port", default="/dev/ttyAMA0")
    args = parser.parse_args()
    print(json.dumps(center(args.port, args.axis), ensure_ascii=False))


if __name__ == "__main__":
    main()
