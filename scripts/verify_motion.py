"""Bounded, on-device acceptance; requires explicit confirmation of clear hardware."""

import argparse
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import serial


def verify(port: str) -> dict[str, Any]:
    """Check bounded factory T1 input, 300ms firmware stop and limited gimbal stop."""
    samples: list[tuple[float, dict[str, Any]]] = []
    with serial.Serial(port, 115200, timeout=0.02, write_timeout=0.1, exclusive=True) as uart:
        lock = threading.RLock()

        def send(command: dict[str, Any]) -> None:
            with lock:
                uart.write((json.dumps(command) + "\n").encode())

        def stop() -> None:
            send({"T": 1, "L": 0.0, "R": 0.0})
            send({"T": 135})

        def collect(duration: float) -> list[tuple[float, dict[str, Any]]]:
            started, last_request = time.monotonic(), 0.0
            result: list[tuple[float, dict[str, Any]]] = []
            while time.monotonic() - started < duration:
                if time.monotonic() - last_request > 0.08:
                    send({"T": 130})
                    last_request = time.monotonic()
                line = uart.readline(4096)
                start = line.find(b'{"T":')
                if start >= 0:
                    try:
                        value = json.loads(line[start:])
                        if value.get("T") == 1001:
                            result.append((time.monotonic(), value))
                    except ValueError:
                        pass
            samples.extend(result)
            return result

        timer = threading.Timer(20, stop)
        timer.start()
        try:
            stop()
            time.sleep(2)
            send({"T": 131, "cmd": 0})
            send({"T": 4, "cmd": 2})
            initial = collect(0.5)
            if not initial or "pan" not in initial[-1][1]:
                raise RuntimeError("No fresh gimbal telemetry")
            send({"T": 136, "cmd": 300})
            commanded_at = time.monotonic()
            send({"T": 1, "L": 0.10, "R": 0.10})
            forward = collect(1.2)
            stop()
            moving = [
                (at, state) for at, state in forward if state["L"] > 0.01 and state["R"] > 0.01
            ]
            stopped = [
                at
                for at, state in forward
                if at > commanded_at + 0.35 and abs(state["L"]) < 0.006 and abs(state["R"]) < 0.006
            ]
            latency = stopped[0] - commanded_at if moving and stopped else None
            send({"T": 1, "L": -0.10, "R": -0.10})
            reverse = collect(0.45)
            stop()
            collect(0.2)
            measured = [state for _, state in reverse if state["L"] < -0.01 and state["R"] < -0.01]
            before = collect(0.2)[-1][1]
            pan, tilt = float(before["pan"]), float(before["tilt"])
            if not -45 <= pan <= 42 or not -15 <= tilt <= 45:
                raise RuntimeError("Current gimbal position outside conservative test envelope")
            send({"T": 133, "X": pan + 3, "Y": tilt, "SPD": 10, "ACC": 5})
            gimbal_move = collect(0.5)
            send({"T": 135})
            settled = collect(0.5)
            moved = bool(gimbal_move) and abs(float(gimbal_move[-1][1]["pan"]) - pan) >= 1
            stable = (
                bool(settled)
                and abs(float(settled[-1][1]["pan"]) - float(settled[0][1]["pan"])) <= 1
            )
            verified_at = time.time()
            return {
                "schema": 1,
                "port": port,
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "verified_at": verified_at,
                "release_policy": "persistent",
                "operator_released_at": verified_at,
                "firmware_timeout_ms": 300,
                "measured_stop_seconds": latency,
                "unit_profile": "t1_scalar_limited",
                "max_command": 0.10,
                "speed_unit": "bounded factory T1 scalar; not a universal m/s command",
                "forward_encoder": [state for _, state in moving[:5]],
                "reverse_encoder": measured[:5],
                "wheels_verified": bool(
                    moving and measured and latency is not None and latency < 0.8
                ),
                "gimbal_verified": moved and stable,
                "pan_before": pan,
                "pan_after": settled[-1][1].get("pan") if settled else None,
                "gimbal_stop_stable": stable,
            }
        finally:
            stop()
            timer.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(description="架空车轮与无障碍云台的受限硬件验收")
    parser.add_argument("--wheels-lifted", action="store_true", required=True)
    parser.add_argument("--gimbal-clear", action="store_true", required=True)
    parser.add_argument("--port", default="/dev/ttyAMA0")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    evidence = verify(args.port)
    args.output.write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
