"""Subscribe to /scan for a while and print an N0 acceptance report as JSON."""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict
from typing import Any

from .scan_summary import is_valid, occluded_arcs, rate_hz, sectors


def collect(seconds: float) -> tuple[list[float], Any]:
    """Return receive times and the last LaserScan; rclpy is only needed here."""
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import LaserScan

    stamps: list[float] = []
    latest: list[Any] = [None]

    def on_scan(message: Any) -> None:
        stamps.append(time.monotonic())
        latest[0] = message

    rclpy.init()
    node = rclpy.create_node("n0_scan_report")
    try:
        node.create_subscription(LaserScan, "scan", on_scan, qos_profile_sensor_data)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return stamps, latest[0]


def report(stamps: list[float], scan: Any, width_deg: int, near_m: float) -> dict[str, Any]:
    result: dict[str, Any] = {"messages": len(stamps), "rate_hz": rate_hz(stamps)}
    if scan is None:
        return result
    found = sectors(scan.ranges, scan.angle_min, scan.angle_increment, width_deg, near_m)
    result.update(
        frame_id=scan.header.frame_id,
        angle_min_deg=round(math.degrees(scan.angle_min), 1),
        angle_max_deg=round(math.degrees(scan.angle_max), 1),
        bins=len(scan.ranges),
        valid_bins=sum(map(is_valid, scan.ranges)),
        occluded_arcs_deg=occluded_arcs(found),
        sectors=[asdict(s) for s in found],
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--sector-deg", type=int, default=15)
    parser.add_argument("--near", type=float, default=0.30, help="closer returns count as body")
    args = parser.parse_args()
    stamps, scan = collect(args.seconds)
    result = report(stamps, scan, args.sector_deg, args.near)
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
