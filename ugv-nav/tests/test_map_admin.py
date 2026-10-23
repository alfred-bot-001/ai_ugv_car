"""Map housekeeping: thumbnails, rename with sidecars, trash instead of delete, default."""

import json
import struct
import zlib
from pathlib import Path

import pytest

from ugv_nav_tools.map_admin import (
    default_map,
    files_of,
    preview,
    rename,
    set_default,
    thumbnail,
    trash,
)
from ugv_nav_tools.map_files import Grid, write_pgm


def write_map(maps: Path, name: str, width: int = 10, height: int = 8) -> None:
    pixels = bytes([254] * (width * height - 4) + [0, 0, 205, 205])
    write_pgm(maps / f"{name}.pgm", width, height, pixels)
    (maps / f"{name}.yaml").write_text(
        f"image: {name}.pgm\nresolution: 0.05\norigin: [-1.0, -2.0, 0]\nnegate: 0\n"
        "occupied_thresh: 0.65\nfree_thresh: 0.196\n"
    )


def png_size(png: bytes) -> tuple[int, int]:
    assert png.startswith(b"\x89PNG")
    return struct.unpack(">II", png[16:24])


def test_thumbnail_keeps_walls_and_fits_the_box() -> None:
    data = [0] * (400 * 300)
    data[150 * 400 + 200] = 100  # a single wall cell survives the 3x downsample
    meta, png = thumbnail(Grid(400, 300, 0.05, 0, 0, data), max_side=160)
    assert png_size(png) == (134, 100) and meta["resolution"] == pytest.approx(0.15)
    raw = zlib.decompress(png[png.index(b"IDAT") + 4 : png.index(b"IEND") - 8])
    assert raw.count(1) >= 1  # palette index 1 is a wall
    small, _ = thumbnail(Grid(4, 3, 0.05, 0, 0, [-1] * 12))
    assert (small["width"], small["height"]) == (4, 3)


def test_preview_reads_a_saved_map(tmp_path: Path) -> None:
    write_map(tmp_path, "room")
    meta, png = preview(tmp_path, "room")
    assert png_size(png) == (10, 8) and meta["origin"] == [-1.0, -2.0]


def test_rename_moves_sidecars_and_follows_references(tmp_path: Path) -> None:
    write_map(tmp_path, "room")
    (tmp_path / "room.places.json").write_text(json.dumps({"version": 1, "map": "room"}))
    (tmp_path / "room.keepout.yaml").write_text("image: room.keepout.pgm\nmode: trinary\n")
    (tmp_path / "room.keepout.pgm").write_bytes(b"P5")
    set_default(tmp_path, "room")
    moved = rename(tmp_path, "room", "study")
    assert sorted(moved) == sorted(
        ["study.yaml", "study.pgm", "study.places.json", "study.keepout.yaml", "study.keepout.pgm"]
    )
    assert files_of(tmp_path, "room") == []
    assert "image: study.pgm" in (tmp_path / "study.yaml").read_text()
    assert "image: study.keepout.pgm" in (tmp_path / "study.keepout.yaml").read_text()
    assert json.loads((tmp_path / "study.places.json").read_text())["map"] == "study"
    assert default_map(tmp_path) == "study"
    preview(tmp_path, "study")  # the renamed map still loads
    write_map(tmp_path, "other")
    with pytest.raises(ValueError, match="已经有"):
        rename(tmp_path, "other", "study")
    with pytest.raises(ValueError, match="找不到"):
        rename(tmp_path, "missing", "x")


def test_trash_keeps_files_and_clears_the_default(tmp_path: Path) -> None:
    write_map(tmp_path, "room")
    set_default(tmp_path, "room")
    folder = trash(tmp_path, "room", now=0)
    assert folder.parent.name == ".trash" and folder.name.startswith("room-")
    assert sorted(p.name for p in folder.iterdir()) == ["room.pgm", "room.yaml"]
    assert files_of(tmp_path, "room") == [] and default_map(tmp_path) is None
    assert oct(folder.parent.stat().st_mode & 0o777) == "0o700"
    with pytest.raises(ValueError):
        trash(tmp_path, "room")
    with pytest.raises(ValueError):
        set_default(tmp_path, "room")
    (tmp_path / "default.json").write_text("not json")
    assert default_map(tmp_path) is None
