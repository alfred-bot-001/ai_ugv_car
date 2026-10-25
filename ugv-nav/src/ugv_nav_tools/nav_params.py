"""Nav2 parameters as launched: the keepout filter only when a map has a keepout mask.

nav.yaml configures the costmaps' KeepoutFilter for maps with <map>.keepout.yaml. With
no mask (every mapping run, every map without zones) the filter still ran and warned
"Filter mask was not received" on every costmap update: 20 712 of 21 613 container log
lines on 2026-10-25. n3_navigation.launch.py passes Nav2 a copy without it then.
"""

from __future__ import annotations

import copy
from typing import Any

COSTMAPS = (("local_costmap", "local_costmap"), ("global_costmap", "global_costmap"))
KEEPOUT = "keepout_filter"


def without_keepout(params: dict[str, Any]) -> dict[str, Any]:
    """A copy of nav.yaml's parameters with the keepout filter taken off both costmaps."""
    result = copy.deepcopy(params)
    for outer, inner in COSTMAPS:
        costmap = result.get(outer, {}).get(inner, {}).get("ros__parameters")
        if not isinstance(costmap, dict):
            continue
        filters = [f for f in costmap.get("filters", []) if f != KEEPOUT]
        if filters:
            costmap["filters"] = filters
        else:
            costmap.pop("filters", None)
        costmap.pop(KEEPOUT, None)
    return result
