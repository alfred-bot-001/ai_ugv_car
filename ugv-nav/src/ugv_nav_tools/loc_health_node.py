"""ROS2 node `ugv-loc-health`: stops the car through pi-agent when localization is unhealthy.

Logic lives in loc_health (unit-tested without ROS). A failing check sends
{"op": "stop", "reason": "localization_lost"}, which also ends pi-agent's arm session;
motion needs healthy localization and a new ugv-navctl arm session. Never relocalizes.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from typing import Any

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav_msgs.msg import OccupancyGrid
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from tf2_ros import Buffer, TransformListener

from .board_client import BoardClient, BoardError
from .bridge_node import SOCKET
from .loc_health import LocalizationHealth, Stamped, match_ratio, scan_endpoints
from .stall import Pose2D

PERIOD_S = 0.2
STOP_REASON = "localization_lost"


def yaw_of(q: Any) -> float:
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


class LocHealthNode(Node):  # type: ignore[misc]
    def __init__(self, localization: bool, client: BoardClient) -> None:
        super().__init__("loc_health")
        self.health = LocalizationHealth(localization)
        self.client = client
        self.tf = Buffer()
        self.listener = TransformListener(self.tf, self)
        self.grid: Any = None
        self.scan: Any = None
        self.ratio: float | None = None
        self.last_reason: str | None = None
        self.board_error = ""
        self.status = self.create_publisher(String, "localization/health", 5)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(OccupancyGrid, "map", self.on_map, latched)
        self.create_subscription(LaserScan, "scan", self.on_scan, qos_profile_sensor_data)
        self.create_subscription(PoseWithCovarianceStamped, "amcl_pose", self.on_amcl, 10)
        self.create_timer(PERIOD_S, self.tick)

    def on_map(self, message: Any) -> None:
        self.grid = message

    def on_scan(self, message: Any) -> None:
        self.scan = message

    def on_amcl(self, message: Any) -> None:
        self.health.on_covariance(list(message.pose.covariance))

    def pose_in_map(self, frame: str) -> Pose2D | None:
        try:
            t = self.tf.lookup_transform("map", frame, Time()).transform
        except Exception:  # noqa: BLE001 - no transform yet means nothing to judge
            return None
        return Pose2D(t.translation.x, t.translation.y, yaw_of(t.rotation))

    def match(self) -> float | None:
        scan, grid = self.scan, self.grid
        if not self.health.localization or scan is None or grid is None:
            return None
        info = grid.info
        if abs(info.origin.orientation.z) > 1e-6:
            return None
        sensor = self.pose_in_map(scan.header.frame_id)
        if sensor is None:
            return None
        points = scan_endpoints(
            scan.ranges,
            scan.angle_min,
            scan.angle_increment,
            sensor,
            scan.range_min,
            scan.range_max,
        )
        return match_ratio(
            points,
            grid.data,
            info.width,
            info.height,
            info.resolution,
            info.origin.position.x,
            info.origin.position.y,
        )

    def tick(self) -> None:
        now = time.monotonic()
        odom = self.pose_in_map("odom")
        if odom is not None:
            self.health.on_transform(Stamped(now, odom))
        self.ratio = self.match()
        self.health.on_match(now, self.ratio)
        reason = self.health.reason
        if reason != self.last_reason:
            if reason:
                self.get_logger().warn(f"unhealthy: {reason} (match {self.ratio})")
            else:
                self.get_logger().info(f"healthy again (match {self.ratio})")
            self.last_reason = reason
        if reason:
            try:
                if self.client.fault() is None:  # also re-halts a fresh arm session
                    self.client.stop(reason=STOP_REASON)
                self.board_error = ""
            except BoardError as error:
                self.board_error = str(error)
        report = {
            "healthy": reason is None,
            "reason": reason,
            "match": self.ratio,
            "localization": self.health.localization,
            "board_error": self.board_error,
        }
        self.status.publish(String(data=json.dumps(report)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--localization", choices=("true", "false"), default="false")
    args, _ = parser.parse_known_args()  # ROS appends its own arguments
    rclpy.init()
    node = LocHealthNode(args.localization == "true", BoardClient(SOCKET))
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
