"""In-container ROS operations for ugv-navctl: pose, set-pose, cancel, navigate.

Prints one JSON object. `navigate` plans first and only sends a NavigateToPose goal if
planning succeeds; it never arms motion (the host writes pi-agent's arm file).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from typing import Any

import rclpy
from action_msgs.srv import CancelGoal
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped
from nav2_msgs.action import ComputePathToPose, NavigateToPose
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.time import Time
from tf2_ros import Buffer, TransformListener

from .board_client import BoardClient, BoardError
from .bridge_node import SOCKET
from .route import Pose, goal_errors, quaternion_to_yaw, within_tolerance, yaw_to_quaternion

SUCCEEDED = 4
SEND_ATTEMPTS = 2
# Humble/FastDDS: a request sent right after discovery can lose its response because the
# reply reader is not matched yet. Spin briefly after discovery before the first call.
MATCH_SETTLE_S = 0.5
CANCEL_ENDPOINTS = (
    "/navigate_to_pose/_action/cancel_goal",
    "/navigate_through_poses/_action/cancel_goal",
    "/follow_path/_action/cancel_goal",
)


class Operator:
    def __init__(self) -> None:
        self.node = Node("ugv_navctl")
        self.tf = Buffer()
        self.listener = TransformListener(self.tf, self.node)

    def spin(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(self.node, timeout_sec=0.05)

    def wait(self, future: Any, timeout: float) -> Any:
        end = time.monotonic() + timeout
        while not future.done() and time.monotonic() < end:
            rclpy.spin_once(self.node, timeout_sec=0.05)
        if not future.done():
            raise TimeoutError("ROS call timed out")
        return future.result()

    def pose(self, timeout: float = 8.0) -> Pose:
        end = time.monotonic() + timeout
        while True:
            try:
                t = self.tf.lookup_transform("map", "base_footprint", Time()).transform
                return Pose(
                    t.translation.x, t.translation.y, quaternion_to_yaw(t.rotation.z, t.rotation.w)
                )
            except Exception:  # noqa: BLE001 - TF not ready yet
                if time.monotonic() > end:
                    raise
                rclpy.spin_once(self.node, timeout_sec=0.05)

    def set_pose(self, pose: Pose) -> Pose:
        publisher = self.node.create_publisher(PoseWithCovarianceStamped, "initialpose", 10)
        self.spin(1.0)
        message = PoseWithCovarianceStamped()
        message.header.frame_id = "map"
        message.header.stamp = self.node.get_clock().now().to_msg()
        message.pose.pose.position.x, message.pose.pose.position.y = pose.x, pose.y
        z, w = yaw_to_quaternion(pose.yaw)
        message.pose.pose.orientation.z, message.pose.pose.orientation.w = z, w
        message.pose.covariance[0] = message.pose.covariance[7] = 0.0025
        message.pose.covariance[35] = 0.01
        publisher.publish(message)
        self.spin(3.0)
        return self.pose()

    def cancel(self) -> dict[str, list[str]]:
        """Best effort: pi-agent's arm file and watchdog, not this call, guarantee a stop."""
        outcome: dict[str, list[str]] = {"cancelled": [], "unanswered": []}
        for endpoint in CANCEL_ENDPOINTS:
            client = self.node.create_client(CancelGoal, endpoint)
            if not client.wait_for_service(timeout_sec=2):
                continue
            self.spin(MATCH_SETTLE_S)
            try:
                self.wait(client.call_async(CancelGoal.Request()), 4)
                outcome["cancelled"].append(endpoint)
            except TimeoutError:
                outcome["unanswered"].append(endpoint)
        return outcome

    def stamped(self, pose: Pose) -> Any:
        goal = PoseStamped()
        goal.header.frame_id = "map"
        goal.header.stamp = self.node.get_clock().now().to_msg()
        goal.pose.position.x, goal.pose.position.y = pose.x, pose.y
        goal.pose.orientation.z, goal.pose.orientation.w = yaw_to_quaternion(pose.yaw)
        return goal

    def navigate(self, goal: Pose, timeout: float) -> dict[str, Any]:
        start = self.pose()
        planner = ActionClient(self.node, ComputePathToPose, "compute_path_to_pose")
        if not planner.wait_for_server(timeout_sec=10):
            raise RuntimeError("planner unavailable")
        self.spin(MATCH_SETTLE_S)
        request = ComputePathToPose.Goal()
        request.goal, request.planner_id, request.use_start = self.stamped(goal), "GridBased", False
        handle = self.wait(planner.send_goal_async(request), 10)
        planned = self.wait(handle.get_result_async(), 15)
        result: dict[str, Any] = {
            "start": asdict(start),
            "goal": asdict(goal),
            "planner_status": planned.status,
            "path_points": len(planned.result.path.poses),
        }
        if planned.status != SUCCEEDED or not planned.result.path.poses:
            result["error"] = "planning failed; motion not attempted"
            return result
        navigator = ActionClient(self.node, NavigateToPose, "navigate_to_pose")
        if not navigator.wait_for_server(timeout_sec=5):
            raise RuntimeError("navigator unavailable")
        self.spin(MATCH_SETTLE_S)
        handle = None
        for attempt in range(1, SEND_ATTEMPTS + 1):
            drive = NavigateToPose.Goal()
            drive.pose = self.stamped(goal)
            try:
                handle = self.wait(navigator.send_goal_async(drive), 10)
                break
            except TimeoutError:
                # Humble can drop the goal response under load; the server may still be
                # running an orphan goal, so cancel everything before trying again.
                result["send_retries"] = attempt
                self.cancel()
        if handle is None:
            result["error"] = "navigator did not answer the goal"
            return result
        if not handle.accepted:
            result["error"] = "navigator rejected goal"
            return result
        future = handle.get_result_async()
        started = time.monotonic()
        try:
            while not future.done() and time.monotonic() - started < timeout:
                rclpy.spin_once(self.node, timeout_sec=0.05)
            if future.done():
                result["navigation_status"] = future.result().status
            else:
                result["timeout"] = True
        finally:
            if not future.done():
                self.wait(handle.cancel_goal_async(), 3)
            try:
                BoardClient(SOCKET).stop()  # always release the chassis
            except BoardError as error:
                result["stop_error"] = str(error)
        reached = self.pose()
        distance, heading = goal_errors(reached, goal)
        result |= {
            "duration_s": round(time.monotonic() - started, 2),
            "reached": asdict(reached),
            "error_m": round(distance, 4),
            "error_rad": round(heading, 4),
            "ok": result.get("navigation_status") == SUCCEEDED and within_tolerance(reached, goal),
        }
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="op", required=True)
    sub.add_parser("pose")
    sub.add_parser("cancel")
    for name in ("set-pose", "navigate"):
        command = sub.add_parser(name)
        command.add_argument("x", type=float)
        command.add_argument("y", type=float)
        command.add_argument("yaw", type=float)
    sub.choices["navigate"].add_argument("--timeout", type=float, default=110.0)
    args = parser.parse_args()
    rclpy.init()
    operator = Operator()
    try:
        if args.op == "pose":
            output: dict[str, Any] = asdict(operator.pose())
        elif args.op == "cancel":
            output = dict(operator.cancel())
        elif args.op == "set-pose":
            output = asdict(operator.set_pose(Pose(args.x, args.y, args.yaw)))
        else:
            if not 1 <= args.timeout <= 110:
                raise ValueError("timeout must be 1..110 s (pi-agent arms at most 120 s)")
            try:
                output = operator.navigate(Pose(args.x, args.y, args.yaw), args.timeout)
            except Exception as error:  # noqa: BLE001 - report every failure as JSON
                output = {"ok": False, "error": f"{type(error).__name__}: {error}"}
                try:
                    BoardClient(SOCKET).stop()
                except BoardError as stop_error:
                    output["stop_error"] = str(stop_error)
    finally:
        operator.node.destroy_node()
        rclpy.shutdown()
    print(json.dumps(output, ensure_ascii=False))
    if args.op == "navigate" and not output.get("ok"):
        sys.exit(2)


if __name__ == "__main__":
    main()
