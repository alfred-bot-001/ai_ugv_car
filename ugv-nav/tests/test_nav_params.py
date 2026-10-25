"""The keepout filter is launched only with a mask; the rest of nav.yaml is untouched."""

from pathlib import Path

from ugv_nav_tools.nav_params import without_keepout

NAV_YAML = Path(__file__).parents[1] / "config" / "nav.yaml"


def costmap(params: dict, name: str) -> dict:
    return params[name][name]["ros__parameters"]


def test_without_keepout_drops_only_the_filter() -> None:
    keepout = {"plugin": "nav2_costmap_2d::KeepoutFilter", "enabled": True}
    params = {
        "local_costmap": {
            "local_costmap": {
                "ros__parameters": {
                    "plugins": ["obstacle_layer"],
                    "filters": ["keepout_filter"],
                    "keepout_filter": keepout,
                }
            }
        },
        "global_costmap": {
            "global_costmap": {
                "ros__parameters": {
                    "filters": ["speed_filter", "keepout_filter"],
                    "keepout_filter": keepout,
                }
            }
        },
        "controller_server": {"ros__parameters": {"controller_frequency": 10.0}},
    }
    result = without_keepout(params)
    assert costmap(result, "local_costmap") == {"plugins": ["obstacle_layer"]}
    assert costmap(result, "global_costmap") == {"filters": ["speed_filter"]}
    assert result["controller_server"] == params["controller_server"]
    assert costmap(params, "local_costmap")["filters"] == ["keepout_filter"]  # a copy
    assert without_keepout({"bt_navigator": {}}) == {"bt_navigator": {}}


def test_nav_yaml_names_the_filter_where_without_keepout_looks() -> None:
    text = NAV_YAML.read_text()
    # Both costmaps list it; nav_params must find it there (the launch drops it by name).
    assert text.count("      filters:\n      - keepout_filter\n") == 2
    assert text.count("      keepout_filter:\n        plugin: nav2_costmap_2d::KeepoutFilter") == 2
