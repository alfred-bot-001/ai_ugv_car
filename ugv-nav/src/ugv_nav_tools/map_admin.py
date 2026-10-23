"""Saved map housekeeping for the map page: thumbnails, rename, delete, default map.

Plain file work in the private maps folder (no ROS). A map is <name>.yaml and its image
plus optional sidecars (places, keepout mask). Delete moves everything into
maps/.trash/<name>-<time>/ so a mistake can be undone by hand; nothing is erased.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from .map_files import Grid, atomic_write, load_grid
from .nav_relay import encode_map

PREVIEW_MAX_SIDE = 160
TRASH = ".trash"
DEFAULT_FILE = "default.json"
# Every file that belongs to a map, by suffix after the map name.
SUFFIXES = (".yaml", ".pgm", ".places.json", ".keepout.yaml", ".keepout.pgm")


def thumbnail(grid: Grid, max_side: int = PREVIEW_MAX_SIDE) -> tuple[dict[str, object], bytes]:
    """North-up palette PNG at most max_side cells wide or tall: walls win, then free."""
    factor = max(1, math.ceil(max(grid.width, grid.height) / max_side))
    width, height = math.ceil(grid.width / factor), math.ceil(grid.height / factor)
    data = []
    for row in range(height):
        for col in range(width):
            value = -1
            for r in range(row * factor, min(grid.height, (row + 1) * factor)):
                for c in range(col * factor, min(grid.width, (col + 1) * factor)):
                    cell = grid.data[r * grid.width + c]
                    if cell >= 65:
                        value = 100
                        break
                    if cell >= 0:
                        value = 0
                if value == 100:
                    break
            data.append(value)
    return encode_map(width, height, data, grid.resolution * factor, grid.origin_x, grid.origin_y)


def preview(maps: Path, name: str) -> tuple[dict[str, object], bytes]:
    return thumbnail(load_grid(maps / f"{name}.yaml"))


def files_of(maps: Path, name: str) -> list[Path]:
    return [maps / f"{name}{suffix}" for suffix in SUFFIXES if (maps / f"{name}{suffix}").exists()]


def default_map(maps: Path) -> str | None:
    try:
        value = json.loads((maps / DEFAULT_FILE).read_text())
    except (OSError, ValueError):
        return None
    name = value.get("map") if isinstance(value, dict) else None
    return name if isinstance(name, str) and (maps / f"{name}.yaml").exists() else None


def set_default(maps: Path, name: str | None) -> None:
    if name is None:
        (maps / DEFAULT_FILE).unlink(missing_ok=True)
        return
    if not (maps / f"{name}.yaml").exists():
        raise ValueError(f"找不到地图 {name}")
    atomic_write(maps / DEFAULT_FILE, json.dumps({"map": name}).encode())


def _point_image(path: Path, old: str, new: str) -> None:
    """Rewrite the yaml's `image:` line from the old map name to the new one."""
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        key, sep, value = line.partition(":")
        if sep and key.strip() == "image" and value.strip().startswith(old):
            lines[i] = f"image: {new}{value.strip()[len(old) :]}"
    atomic_write(path, ("\n".join(lines) + "\n").encode())


def rename(maps: Path, old: str, new: str) -> list[str]:
    """Rename a map and its sidecars; the yaml image lines and places file follow."""
    if not (maps / f"{old}.yaml").exists():
        raise ValueError(f"找不到地图 {old}")
    if files_of(maps, new):
        raise ValueError(f"已经有叫 {new} 的地图了")
    moved: list[tuple[Path, Path]] = []
    try:
        for source in files_of(maps, old):
            target = maps / (new + source.name[len(old) :])
            os.rename(source, target)
            moved.append((source, target))
    except OSError:
        for source, target in reversed(moved):
            os.rename(target, source)
        raise
    for suffix in (".yaml", ".keepout.yaml"):
        if (maps / f"{new}{suffix}").exists():
            _point_image(maps / f"{new}{suffix}", old, new)
    places = maps / f"{new}.places.json"
    if places.exists():
        value = json.loads(places.read_text())
        value["map"] = new
        atomic_write(places, json.dumps(value, ensure_ascii=False, indent=1).encode())
    if default_map(maps) is None and _default_name(maps) == old:
        set_default(maps, new)
    return [target.name for _, target in moved]


def _default_name(maps: Path) -> str | None:
    try:
        value = json.loads((maps / DEFAULT_FILE).read_text())
    except (OSError, ValueError):
        return None
    return value.get("map") if isinstance(value, dict) else None


def trash(maps: Path, name: str, now: float | None = None) -> Path:
    """Move a map and its sidecars into maps/.trash/<name>-<time>/ (0700)."""
    files = files_of(maps, name)
    if not (maps / f"{name}.yaml").exists():
        raise ValueError(f"找不到地图 {name}")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    folder = maps / TRASH / f"{name}-{stamp}"
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    (maps / TRASH).chmod(0o700)
    for source in files:
        os.rename(source, folder / source.name)
    if _default_name(maps) == name:
        set_default(maps, None)
    return folder
