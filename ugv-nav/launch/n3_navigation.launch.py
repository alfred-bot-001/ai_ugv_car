"""N2/N3: N1 odometry plus SLAM Toolbox mapping or AMCL localization, and Nav2.

mapping:=true      online SLAM builds /map (drive manually, then save with ugv-navctl)
localization:=true map_server + AMCL on a saved map
navigation:=true   Nav2 planner/controller; /cmd_vel still passes the bridge gate and
                   pi-agent, and moves only while ugv-navctl holds a bounded arm window.
"""

import tempfile

import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchContext, LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    EmitEvent,
    ExecuteProcess,
    IncludeLaunchDescription,
    OpaqueFunction,
    RegisterEventHandler,
)
from launch.conditions import IfCondition
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node

from ugv_nav_tools.nav_params import without_keepout

CONFIG = "/app/config"
COMMON = {"use_sim_time": "false", "autostart": "true", "use_composition": "False"}


def keepout_servers() -> list[Node]:
    """Mask and filter-info servers for the costmaps' keepout filter (S0 §3.3)."""
    has_mask = IfCondition(PythonExpression(["'", LaunchConfiguration("keepout"), "' != ''"]))
    return [
        Node(
            package="nav2_map_server",
            executable="map_server",
            name="filter_mask_server",
            parameters=[f"{CONFIG}/nav.yaml", {"yaml_filename": LaunchConfiguration("keepout")}],
            condition=has_mask,
            output="screen",
        ),
        Node(
            package="nav2_map_server",
            executable="costmap_filter_info_server",
            name="costmap_filter_info_server",
            parameters=[f"{CONFIG}/nav.yaml"],
            condition=has_mask,
            output="screen",
        ),
        Node(
            package="nav2_lifecycle_manager",
            executable="lifecycle_manager",
            name="lifecycle_manager_costmap_filters",
            parameters=[
                {
                    "use_sim_time": False,
                    "autostart": True,
                    "node_names": ["filter_mask_server", "costmap_filter_info_server"],
                }
            ],
            condition=has_mask,
            output="screen",
        ),
    ]


def navigation(context: LaunchContext) -> list[IncludeLaunchDescription]:
    """Nav2 on nav.yaml; with no keepout mask, on a copy without the keepout filter."""
    params = f"{CONFIG}/nav.yaml"
    if not LaunchConfiguration("keepout").perform(context):
        with open(params, encoding="utf-8") as source:
            values = without_keepout(yaml.safe_load(source))
        with tempfile.NamedTemporaryFile("w", prefix="nav-", suffix=".yaml", delete=False) as out:
            yaml.safe_dump(values, out)
            params = out.name
    nav2 = get_package_share_directory("nav2_bringup") + "/launch"
    return [
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(f"{nav2}/navigation_launch.py"),
            launch_arguments={"params_file": params, **COMMON}.items(),
            condition=IfCondition(LaunchConfiguration("navigation")),
        )
    ]


def generate_launch_description() -> LaunchDescription:
    nav2 = get_package_share_directory("nav2_bringup") + "/launch"
    slam = get_package_share_directory("slam_toolbox") + "/launch/online_async_launch.py"
    return LaunchDescription(
        [
            # Any component exiting tears the stack down; a half-running Nav2 is worse.
            RegisterEventHandler(
                OnProcessExit(on_exit=[EmitEvent(event=Shutdown(reason="component exited"))])
            ),
            DeclareLaunchArgument("mapping", default_value="true"),
            DeclareLaunchArgument("localization", default_value="false"),
            DeclareLaunchArgument("navigation", default_value="true"),
            DeclareLaunchArgument("map", default_value="/maps/map.yaml"),
            DeclareLaunchArgument("keepout", default_value=""),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource("/app/launch/n1_odometry.launch.py")
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(slam),
                launch_arguments={
                    "slam_params_file": f"{CONFIG}/slam.yaml",
                    "use_sim_time": "false",
                }.items(),
                condition=IfCondition(LaunchConfiguration("mapping")),
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(f"{nav2}/localization_launch.py"),
                launch_arguments={
                    "map": LaunchConfiguration("map"),
                    "params_file": f"{CONFIG}/nav.yaml",
                    **COMMON,
                }.items(),
                condition=IfCondition(LaunchConfiguration("localization")),
            ),
            # S0 localization health: stops the car through pi-agent on a jump, a poor
            # scan-to-map match or wide AMCL covariance (the last two only on a saved map).
            ExecuteProcess(
                cmd=["ugv-loc-health", "--localization", LaunchConfiguration("localization")],
                name="loc_health",
                output="screen",
            ),
            # Read-only feed for the console's map page (map, pose, scan, plan, logs) through
            # pi-agent's nav/stream.sock; it never publishes.
            ExecuteProcess(
                cmd=[
                    "ugv-nav-relay",
                    "--mapping",
                    LaunchConfiguration("mapping"),
                    "--map",
                    LaunchConfiguration("map"),
                ],
                name="nav_relay",
                output="screen",
            ),
            *keepout_servers(),
            OpaqueFunction(function=navigation),
        ]
    )
