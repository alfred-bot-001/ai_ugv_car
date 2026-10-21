"""Saved map files (map_saver's PGM + YAML) and the keepout mask derived from them.

Pure file logic, no ROS. Grids follow nav_msgs/OccupancyGrid: row 0 is the bottom row
at the map origin, values 0 free, 100 occupied, -1 unknown. PGM rows run top-down.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# map_saver_cli is run with these (see navctl.save); the mask uses the same values.
OCCUPIED_THRESH = 0.65
FREE_THRESH = 0.196
FREE_PIXEL = 254
OCCUPIED_PIXEL = 0
MAX_CELLS = 4_000_000  # 100 m x 100 m at 5 cm; anything larger is not a home map


@dataclass(frozen=True)
class MapInfo:
    image: str
    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float
    negate: bool
    occupied_thresh: float
    free_thresh: float


@dataclass(frozen=True)
class Grid:
    width: int
    height: int
    resolution: float
    origin_x: float
    origin_y: float
    data: list[int]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_map_yaml(path: Path) -> MapInfo:
    """The flat key: value YAML map_saver writes; anything unexpected is rejected."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"unexpected map yaml line: {line!r}")
        values[key.strip()] = value.strip()
    try:
        origin = [float(v) for v in values["origin"].strip("[]").split(",")]
        info = MapInfo(
            image=values["image"],
            resolution=float(values["resolution"]),
            origin_x=origin[0],
            origin_y=origin[1],
            origin_yaw=origin[2],
            negate=values.get("negate", "0") not in ("0", "false", "False"),
            occupied_thresh=float(values.get("occupied_thresh", OCCUPIED_THRESH)),
            free_thresh=float(values.get("free_thresh", FREE_THRESH)),
        )
    except (KeyError, IndexError, ValueError) as error:
        raise ValueError(f"invalid map yaml: {error}") from error
    numbers = (info.resolution, info.origin_x, info.origin_y, info.origin_yaw)
    if info.resolution <= 0 or not all(map(math.isfinite, numbers)) or "/" in info.image:
        raise ValueError("invalid map yaml values")
    return info


def read_pgm(path: Path) -> tuple[int, int, bytes]:
    """Binary (P5) 8-bit PGM: width, height and top-down pixels."""
    raw = path.read_bytes()
    tokens: list[bytes] = []
    position = 0
    while len(tokens) < 4:
        match = re.compile(rb"\s*(#[^\n]*\n\s*)*(\S+)").match(raw, position)
        if not match:
            raise ValueError("truncated PGM header")
        tokens.append(match.group(2))
        position = match.end()
    if tokens[0] != b"P5":
        raise ValueError("only binary P5 PGM maps are supported")
    width, height, maxval = (int(t) for t in tokens[1:])
    if not 0 < width * height <= MAX_CELLS or maxval != 255:
        raise ValueError("unsupported PGM size or depth")
    pixels = raw[position + 1 : position + 1 + width * height]
    if len(pixels) != width * height:
        raise ValueError("truncated PGM data")
    return width, height, pixels


def write_pgm(path: Path, width: int, height: int, pixels: bytes) -> None:
    atomic_write(path, f"P5\n{width} {height}\n255\n".encode() + pixels)


def atomic_write(path: Path, data: bytes) -> None:
    """Private 0600 file replaced atomically."""
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


def load_grid(yaml_path: Path) -> Grid:
    """Occupancy grid of a saved map, as Nav2's map_server would publish it (trinary)."""
    info = read_map_yaml(yaml_path)
    if abs(info.origin_yaw) > 1e-6:
        raise ValueError("rotated map origins are not supported")
    width, height, pixels = read_pgm(yaml_path.parent / info.image)
    data = [0] * (width * height)
    for row in range(height):
        top_down = (height - 1 - row) * width
        for column in range(width):
            pixel = pixels[top_down + column]
            p = pixel / 255 if info.negate else (255 - pixel) / 255
            if p > info.occupied_thresh:
                value = 100
            elif p < info.free_thresh:
                value = 0
            else:
                value = -1
            data[row * width + column] = value
    return Grid(width, height, info.resolution, info.origin_x, info.origin_y, data)


def inside(x: float, y: float, polygon: Sequence[tuple[float, float]]) -> bool:
    """Even-odd ray casting; points on an edge may fall either way."""
    result = False
    for (x1, y1), (x2, y2) in zip(polygon, [*polygon[1:], polygon[0]], strict=False):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            result = not result
    return result


def keepout_pixels(
    width: int,
    height: int,
    resolution: float,
    origin_x: float,
    origin_y: float,
    polygons: Sequence[Sequence[tuple[float, float]]],
) -> bytes:
    """Top-down PGM pixels: occupied where a cell centre lies inside any keepout polygon."""
    pixels = bytearray([FREE_PIXEL]) * (width * height)
    for polygon in polygons:
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        col_lo = max(0, math.floor((min(xs) - origin_x) / resolution))
        col_hi = min(width - 1, math.floor((max(xs) - origin_x) / resolution))
        row_lo = max(0, math.floor((min(ys) - origin_y) / resolution))
        row_hi = min(height - 1, math.floor((max(ys) - origin_y) / resolution))
        for row in range(row_lo, row_hi + 1):
            cy = origin_y + (row + 0.5) * resolution
            for column in range(col_lo, col_hi + 1):
                cx = origin_x + (column + 0.5) * resolution
                if inside(cx, cy, polygon):
                    pixels[(height - 1 - row) * width + column] = OCCUPIED_PIXEL
    return bytes(pixels)


def write_keepout_mask(
    yaml_path: Path, polygons: Sequence[Sequence[tuple[float, float]]]
) -> tuple[Path, int]:
    """Write <map>.keepout.pgm/.yaml aligned with the map; returns the yaml and cell count."""
    info = read_map_yaml(yaml_path)
    width, height, _ = read_pgm(yaml_path.parent / info.image)
    pixels = keepout_pixels(width, height, info.resolution, info.origin_x, info.origin_y, polygons)
    stem = yaml_path.name.removesuffix(".yaml")
    image = yaml_path.with_name(f"{stem}.keepout.pgm")
    write_pgm(image, width, height, pixels)
    mask = yaml_path.with_name(f"{stem}.keepout.yaml")
    text = (
        f"image: {image.name}\nmode: trinary\nresolution: {info.resolution}\n"
        f"origin: [{info.origin_x}, {info.origin_y}, {info.origin_yaw}]\n"
        f"negate: 0\noccupied_thresh: {OCCUPIED_THRESH}\nfree_thresh: {FREE_THRESH}\n"
    )
    atomic_write(mask, text.encode())
    return mask, pixels.count(OCCUPIED_PIXEL)
