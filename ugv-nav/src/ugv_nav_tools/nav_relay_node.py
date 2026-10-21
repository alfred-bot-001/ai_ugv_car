"""ROS2 node `ugv-nav-relay`: live map, pose, scan, path, state and logs for the map page.

Read-only: it subscribes and looks up TF, never publishes. Encoding and the stream
sender live in nav_relay (unit-tested without ROS).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from typing import Any

import rclpy
from nav_msgs.msg import OccupancyGrid
from nav_msgs.msg import Path as PlanMsg
from rcl_interfaces.msg import Log
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from tf2_ros import Buffer, TransformListener

from .loc_health_node import yaw_of
from .nav_relay import (
    LogFilter,
    StreamSender,
    compose,
    encode_map,
    packet,
    path_points,
    scan_points,
)
from .stall import Pose2D

MAP_PERIOD_S = 1.0
POSE_PERIOD_S = 0.1
SCAN_PERIOD_S = 0.2
STATE_PERIOD_S = 1.0
LOGS_PER_SECOND = 20


class NavRelay(Node):  # type: ignore[misc]
    def __init__(self, mode: str, map_name: str, sender: StreamSender) -> None:
        super().__init__("nav_relay")
        self.mode = mode
        self.map_name = map_name
        self.sender = sender
        self.tf = Buffer()
        self.listener = TransformListener(self.tf, self)
        self.logs = LogFilter()
        self.pending_map: Any = None
        self.map_sent_at = 0.0
        self.map_stamp = 0.0
        self.scan: Any = None
        self.scan_count = 0
        self.scan_rate = 0.0
        self.scan_rate_at = time.monotonic()
        self.health: dict[str, Any] | None = None
        self.health_at = 0.0
        self.last_pose: Pose2D | None = None
        self.pose_sent_at = 0.0
        self.log_budget = float(LOGS_PER_SECOND)
        self.log_budget_at = time.monotonic()
        self.dropped_logs = 0
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(OccupancyGrid, "map", self.on_map, latched)
        self.create_subscription(LaserScan, "scan", self.on_scan, qos_profile_sensor_data)
        self.create_subscription(PlanMsg, "plan", self.on_plan, 5)
        self.create_subscription(String, "localization/health", self.on_health, 5)
        self.create_subscription(Log, "rosout", self.on_log, 200)
        self.create_timer(0.5, self.send_map)
        self.create_timer(POSE_PERIOD_S, self.send_pose)
        self.create_timer(SCAN_PERIOD_S, self.send_scan)
        self.create_timer(STATE_PERIOD_S, self.send_state)

    def lookup(self, parent: str, child: str) -> Pose2D | None:
        try:
            t = self.tf.lookup_transform(parent, child, Time()).transform
        except Exception:  # noqa: BLE001 - a missing transform just means nothing to show
            return None
        return Pose2D(t.translation.x, t.translation.y, yaw_of(t.rotation))

    def in_map(self, frame: str) -> Pose2D | None:
        """map->odom and odom->frame chained from their latest values.

        One map->frame lookup fails after ~10 s parked in mapping mode, because SLAM
        stops refreshing map->odom while the car stands still.
        """
        map_odom, odom_frame = self.lookup("map", "odom"), self.lookup("odom", frame)
        if map_odom is None or odom_frame is None:
            return None
        return compose(map_odom, odom_frame)

    def on_map(self, message: Any) -> None:
        self.pending_map = message

    def send_map(self) -> None:
        message, now = self.pending_map, time.monotonic()
        if message is None or now - self.map_sent_at < MAP_PERIOD_S:
            return
        self.pending_map = None
        self.map_sent_at = now
        info = message.info
        if abs(info.origin.orientation.z) > 1e-6:
            self.get_logger().warn("rotated map origin is not supported by the map page")
            return
        meta, png = encode_map(
            info.width,
            info.height,
            message.data,
            info.resolution,
            info.origin.position.x,
            info.origin.position.y,
        )
        self.map_stamp = time.time()
        self.sender.put("map", packet("map", {"stamp": self.map_stamp, **meta}, png))

    def on_scan(self, message: Any) -> None:
        self.scan = message
        self.scan_count += 1

    def send_scan(self) -> None:
        scan = self.scan
        if scan is None:
            return
        self.scan = None
        sensor = self.in_map(scan.header.frame_id)
        if sensor is None:
            return
        points = scan_points(
            scan.ranges,
            scan.angle_min,
            scan.angle_increment,
            sensor,
            scan.range_min,
            scan.range_max,
        )
        self.sender.put("scan", packet("scan", {"stamp": time.time(), "points": points}))

    def send_pose(self) -> None:
        pose = self.in_map("base_footprint")
        if pose is None:
            return
        now = time.monotonic()
        last = self.last_pose
        moved = last is None or (
            abs(pose.x - last.x) > 0.002
            or abs(pose.y - last.y) > 0.002
            or abs(pose.yaw - last.yaw) > 0.003
        )
        if not moved and now - self.pose_sent_at < 1.0:
            return
        self.last_pose, self.pose_sent_at = pose, now
        meta = {"stamp": time.time(), "x": round(pose.x, 3), "y": round(pose.y, 3)}
        meta["yaw"] = round(pose.yaw, 4)
        self.sender.put("pose", packet("pose", meta))

    def on_plan(self, message: Any) -> None:
        if message.header.frame_id not in ("", "map"):
            return
        points = [(p.pose.position.x, p.pose.position.y) for p in message.poses]
        meta = {"stamp": time.time(), "points": path_points(points)}
        self.sender.put("plan", packet("plan", meta))

    def on_health(self, message: Any) -> None:
        try:
            value = json.loads(message.data)
        except ValueError:
            return
        if isinstance(value, dict):
            self.health, self.health_at = value, time.time()

    def send_state(self) -> None:
        now = time.monotonic()
        elapsed = max(now - self.scan_rate_at, 1e-3)
        self.scan_rate = self.scan_count / elapsed
        self.scan_count, self.scan_rate_at = 0, now
        meta = {
            "stamp": time.time(),
            "mode": self.mode,
            "map": self.map_name,
            "map_stamp": self.map_stamp or None,
            "health": self.health,
            "health_stamp": self.health_at or None,
            "scan_hz": round(self.scan_rate, 1),
            "dropped_logs": self.dropped_logs,
        }
        self.sender.put("state", packet("state", meta))

    def on_log(self, message: Any) -> None:
        level = message.level
        level = level[0] if isinstance(level, bytes) else int(level)
        now = time.monotonic()
        self.log_budget = min(
            float(LOGS_PER_SECOND),
            self.log_budget + (now - self.log_budget_at) * LOGS_PER_SECOND,
        )
        self.log_budget_at = now
        line = self.logs.accept(now, level, str(message.name), str(message.msg))
        if line is None:
            return
        if self.log_budget < 1:
            self.dropped_logs += 1
            return
        self.log_budget -= 1
        stamp = message.stamp.sec + message.stamp.nanosec * 1e-9
        self.sender.put("log", packet("log", {"stamp": round(stamp, 3), **line}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", choices=("true", "false"), default="true")
    parser.add_argument("--map", default="")
    args, _ = parser.parse_known_args()  # ROS appends its own arguments
    mapping = args.mapping == "true"
    sender = StreamSender()
    thread = threading.Thread(target=sender.run, daemon=True, name="nav-relay-sender")
    thread.start()
    rclpy.init()
    node = NavRelay(
        "mapping" if mapping else "localization",
        "" if mapping else Path(args.map).stem,
        sender,
    )
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        sender.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
