"""N0: LiDAR /scan and the Rover TF tree. Nothing here can move the robot."""

import os

import xacro
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    share = get_package_share_directory("ugv_description")
    # Vendor xacro includes the OAK camera *unless* add_depth_camera is true; we have no OAK.
    urdf = xacro.process_file(
        os.path.join(share, "urdf/bases/ugv_rover.xacro"),
        mappings={"use_gazebo": "false", "add_depth_camera": "true"},
    ).toxml()
    return LaunchDescription(
        [
            DeclareLaunchArgument("port_name", default_value="/dev/ugv_lidar"),
            # N0 keeps the full 360 degrees so the body occlusion can be measured.
            DeclareLaunchArgument("enable_angle_crop_func", default_value="false"),
            Node(
                package="ldlidar",
                executable="ldlidar_node",
                name="ldlidar",
                output="screen",
                parameters=[
                    {
                        "product_name": "LDLiDAR_LD19",
                        "topic_name": "scan",
                        "frame_id": "base_lidar_link",
                        "port_name": LaunchConfiguration("port_name"),
                        "port_baudrate": 230400,
                        "laser_scan_dir": True,
                        "enable_angle_crop_func": LaunchConfiguration("enable_angle_crop_func"),
                        "angle_crop_min": 225.0,
                        "angle_crop_max": 315.0,
                        "bins": 480,
                    }
                ],
            ),
            Node(
                package="robot_state_publisher",
                executable="robot_state_publisher",
                parameters=[{"robot_description": urdf}],
            ),
            Node(package="joint_state_publisher", executable="joint_state_publisher"),
        ]
    )
