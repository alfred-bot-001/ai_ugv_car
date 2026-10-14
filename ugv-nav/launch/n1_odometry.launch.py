"""N1: N0 plus the pi-agent bridge, RF2O laser odometry and the laser EKF (odom -> base)."""

from launch import LaunchDescription
from launch.actions import ExecuteProcess, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription(
        [
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource("/app/launch/n0_lidar.launch.py")
            ),
            # Telemetry out, gated /cmd_vel in; motion still needs pi-agent's arm file.
            ExecuteProcess(cmd=["ugv-board-bridge"], name="board_bridge", output="screen"),
            Node(
                package="rf2o_laser_odometry",
                executable="rf2o_laser_odometry_node",
                parameters=[
                    {
                        "laser_scan_topic": "/scan",
                        "odom_topic": "/odom_rf2o",
                        "publish_tf": False,
                        "base_frame_id": "base_footprint",
                        "odom_frame_id": "odom",
                        "init_pose_from_topic": "",
                        "freq": 10.0,
                    }
                ],
                output="screen",
            ),
            Node(
                package="robot_localization",
                executable="ekf_node",
                name="ekf_laser",
                parameters=["/app/config/ekf_laser.yaml"],
                remappings=[("odometry/filtered", "odom")],
                output="screen",
            ),
        ]
    )
