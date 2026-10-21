"""ROS2 node between Nav2 and pi-agent: telemetry out, gated /cmd_vel in.

Logic lives in board_client, wheel_odometry and motion_gate (unit-tested without ROS);
this module only wires them to topics. Run inside the ugv-nav container.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

import rclpy
from geometry_msgs.msg import Twist
from nav_msgs.msg import OccupancyGrid, Odometry
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import BatteryState, LaserScan
from std_msgs.msg import String
from tf2_ros import Buffer, TransformListener

from .board_client import BoardClient, BoardError
from .motion_gate import STALE_SECONDS, Perception, front_ranges, gate, rotation_space_clear
from .stall import BodyStallWatch, Pose2D, Sample, motion
from .wheel_odometry import WheelOdometry

SOCKET = Path("/board/board.sock")
HEALTH_FILE = Path("/tmp/board_health.json")
TELEMETRY_MAX_AGE = 0.3
REQUIRED = ("odl", "odr", "L", "R", "v")
STALL_NOTE = "stalled: wheels turning, body still"


class BoardBridge(Node):  # type: ignore[misc]
    def __init__(self, client: BoardClient) -> None:
        super().__init__("board_bridge")
        self.client = client
        self.wheels = WheelOdometry()
        self.odom = self.create_publisher(Odometry, "odom_wheel", 20)
        self.battery = self.create_publisher(BatteryState, "ugv/voltage", 5)
        self.status = self.create_publisher(String, "board/status", 5)
        self.create_subscription(Twist, "cmd_vel", self.on_cmd_vel, 10)
        self.create_subscription(Odometry, "odom_rf2o", self.on_laser_odom, 10)
        self.create_subscription(LaserScan, "scan", self.on_scan, qos_profile_sensor_data)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(OccupancyGrid, "map", self.on_map, latched)
        self.tf = Buffer()
        self.listener = TransformListener(self.tf, self)
        self.seen = Perception(scan_at=0.0, front_count=0, front_min=0.0, laser_odom_at=0.0)
        self.map: Any = None
        self.map_at = 0.0
        self.last_sample = 0.0
        self.last_status = 0.0
        self.command_note = ""
        self.stall_watch = BodyStallWatch()
        self.wheel_pose: Pose2D | None = None
        self.laser_pose: Pose2D | None = None
        self.laser_pose_at = 0.0
        self.create_timer(0.05, self.poll)

    def on_laser_odom(self, message: Any) -> None:
        p, q = message.pose.pose.position, message.pose.pose.orientation
        age = abs((self.get_clock().now() - Time.from_msg(message.header.stamp)).nanoseconds) * 1e-9
        unit = abs(q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w - 1) < 0.01
        if age < 0.35 and unit and all(map(math.isfinite, (p.x, p.y, q.x, q.y, q.z, q.w))):
            now = time.monotonic()
            self.seen = Perception(
                self.seen.scan_at, self.seen.front_count, self.seen.front_min, now
            )
            yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
            self.laser_pose, self.laser_pose_at = Pose2D(p.x, p.y, yaw), now

    def on_scan(self, message: Any) -> None:
        front = front_ranges(message.ranges, message.angle_min, message.angle_increment)
        self.seen = Perception(
            time.monotonic(), len(front), min(front, default=0.0), self.seen.laser_odom_at
        )

    def on_map(self, message: Any) -> None:
        self.map, self.map_at = message, time.monotonic()

    def turn_clear(self) -> bool:
        grid = self.map
        if grid is None or time.monotonic() - self.map_at > 5 or grid.header.frame_id != "map":
            return False
        if abs(grid.info.origin.orientation.z) > 1e-6:
            return False
        try:
            pose = self.tf.lookup_transform("map", "base_footprint", Time())
        except Exception:  # noqa: BLE001 - any TF failure means "not verified"
            return False
        age = abs((self.get_clock().now() - Time.from_msg(pose.header.stamp)).nanoseconds) * 1e-9
        if age > 0.4:
            return False
        t = pose.transform.translation
        info = grid.info
        return rotation_space_clear(
            grid.data,
            info.width,
            info.height,
            info.resolution,
            info.origin.position.x,
            info.origin.position.y,
            t.x,
            t.y,
        )

    def on_cmd_vel(self, message: Any) -> None:
        try:
            planar = (message.linear.y, message.linear.z, message.angular.x, message.angular.y)
            if any(abs(v) > 1e-9 for v in planar):
                raise ValueError("non-planar velocity rejected")
            decision = gate(
                message.linear.x, message.angular.z, time.monotonic(), self.seen, self.turn_clear
            )
            if decision.stop:
                self.client.stop()
                raise ValueError(decision.reason)
            self.client.command(decision.linear, decision.angular)
            self.command_note = decision.reason
        except (BoardError, ValueError) as error:
            self.command_note = str(error)

    def poll(self) -> None:
        now = time.monotonic()
        try:
            data = self.client.status()
            telemetry = data["telemetry"]
            values = [telemetry.get(k) for k in REQUIRED]
            valid = all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)
            if data["age"] is None or data["age"] > TELEMETRY_MAX_AGE or not valid:
                raise ValueError("stale or invalid hardware telemetry")
            if data["sampled_monotonic"] != self.last_sample:
                self.last_sample = data["sampled_monotonic"]
                self.publish(telemetry)
                self.check_stall(now)
            if now - self.last_status > 1:
                data |= {
                    "command_note": self.command_note,
                    "laser_odom_age": now - self.seen.laser_odom_at,
                    "scan_age": now - self.seen.scan_at,
                    "front_min": self.seen.front_min,
                    "wheel_rejected": self.wheels.rejected,
                }
                HEALTH_FILE.write_text(json.dumps({"monotonic": now, "age": data["age"]}))
                self.status.publish(String(data=json.dumps(data)))
                self.last_status = now
        except (BoardError, ValueError, KeyError, TypeError) as error:
            if now - self.last_status > 1:
                self.status.publish(String(data=json.dumps({"ok": False, "error": str(error)})))
                self.last_status = now

    def check_stall(self, now: float) -> None:
        """Stop and end the arm session when the wheels turn but the body stays put.

        pi-agent refuses the rest of that arm session, so no latch is kept here; the
        window restarts and a stall in a new session needs another full window.
        """
        laser = self.laser_pose
        if self.wheel_pose is None or laser is None or now - self.laser_pose_at > STALE_SECONDS:
            self.stall_watch.reset()  # the motion gate already stops on stale laser odometry
            return
        if self.stall_watch.add(Sample(now, self.wheel_pose, laser)):
            details = {k: round(v, 3) for k, v in motion(list(self.stall_watch.samples)).items()}
            self.command_note = f"{STALL_NOTE} {json.dumps(details)}"
            self.get_logger().warning(self.command_note)
            self.stall_watch.reset()
            self.client.stop(reason="stall_body")

    def publish(self, telemetry: dict[str, Any]) -> None:
        pose = self.wheels.update(
            float(telemetry["odl"]),
            float(telemetry["odr"]),
            float(telemetry["L"]),
            float(telemetry["R"]),
        )
        stamp = self.get_clock().now().to_msg()
        odom = Odometry()
        odom.header.stamp = stamp
        odom.header.frame_id = "odom"
        odom.child_frame_id = "base_footprint"
        odom.pose.pose.position.x, odom.pose.pose.position.y = pose.x, pose.y
        odom.pose.pose.orientation.z = math.sin(pose.yaw / 2)
        odom.pose.pose.orientation.w = math.cos(pose.yaw / 2)
        odom.twist.twist.linear.x, odom.twist.twist.angular.z = pose.linear, pose.angular
        self.odom.publish(odom)
        self.wheel_pose = Pose2D(pose.x, pose.y, pose.yaw)
        battery = BatteryState()
        battery.header.stamp = stamp
        battery.header.frame_id = "base_link"
        battery.voltage = float(telemetry["v"])
        battery.present = True
        battery.percentage = math.nan
        self.battery.publish(battery)


def main() -> None:
    rclpy.init()
    client = BoardClient(SOCKET)
    node = BoardBridge(client)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            client.stop()
        except BoardError:
            pass
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
