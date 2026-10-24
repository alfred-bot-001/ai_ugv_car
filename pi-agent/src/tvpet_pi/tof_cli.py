"""tvpet-tof: record the floor baseline or watch the ToF matrix; stop the agent first.

pi-agent is the bridge port's only owner, so like `tvpet-gimbal-center` this runs
with `ai-tv-pet-agent.service` stopped. The baseline is runtime data and goes to the
agent-data directory (tof.DEFAULT_BASELINE), never into the deploy tree.
"""

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .tof import (
    COLUMNS,
    DEFAULT_BASELINE,
    INVALID_MM,
    STARTUP_GRACE,
    Baseline,
    ToFLink,
    median_baseline,
)


def grid(values: tuple[int, ...]) -> str:
    """The 8x8 matrix as the sensor sees it: row y0 first, x0 on the left, '-' = 4000."""
    rows = []
    for row in range(len(values) // COLUMNS):
        cells = values[row * COLUMNS : (row + 1) * COLUMNS]
        rows.append(" ".join("   -" if v >= INVALID_MM else f"{v:4d}" for v in cells))
    return "\n".join(rows)


def calibrate(
    link: ToFLink,
    path: Path,
    frames: int,
    timeout: float,
    out: Callable[[str], None] = print,
) -> Baseline:
    """Car still on level floor: the median of `frames` frames becomes the 64-cell baseline."""
    samples = link.collect(frames, timeout)
    baseline = Baseline(median_baseline(samples), time.time(), len(samples))
    baseline.save(path)
    out(f"baseline from {len(samples)} frames -> {path} ({sum(baseline.ground())} floor cells)")
    out(grid(baseline.values))
    return baseline


def watch(link: ToFLink, seconds: float, out: Callable[[str], None] = print) -> None:
    """Print each new frame with the detector's verdict (bench and on-car commissioning)."""
    deadline = time.monotonic() + seconds
    last: int | None = None
    while time.monotonic() < deadline:
        status = link.status()
        if status["values"] is not None and status["seq"] != last:
            last = status["seq"]
            out(
                f"seq {status['seq']} {status['hz']} Hz state={status['state']} "
                f"baseline={status['baseline']} reason={status['reason_text'] or '-'} "
                f"obstacle={status['obstacle_cells']} cliff={status['cliff_cells']}"
            )
            out(grid(tuple(status["values"])))
        time.sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SEN0628 ToF matrix through the XIAO bridge (stop ai-tv-pet-agent first)"
    )
    parser.add_argument("--port", required=True, help="/dev/serial/by-id/... of the XIAO bridge")
    parser.add_argument("--baseline", type=Path, default=Path(DEFAULT_BASELINE).expanduser())
    commands = parser.add_subparsers(dest="command", required=True)
    calibration = commands.add_parser("calibrate", help="record the floor baseline on level ground")
    calibration.add_argument("--frames", type=int, default=30)
    calibration.add_argument("--timeout", type=float, default=STARTUP_GRACE + 15)
    watching = commands.add_parser("watch", help="print frames and the detector verdict")
    watching.add_argument("--seconds", type=float, default=30)
    args = parser.parse_args(argv)
    link = ToFLink(args.port, args.baseline)
    link.open()
    try:
        if args.command == "calibrate":
            calibrate(link, args.baseline, args.frames, args.timeout)
        else:
            watch(link, args.seconds)
    except TimeoutError as error:
        print(f"error: {error} (state={link.state()}, port={link.error or 'ok'})", file=sys.stderr)
        return 1
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
