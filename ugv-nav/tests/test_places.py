"""Saved-map files, keepout masks, the places file and the navctl commands that edit it."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from ugv_nav_tools import places as placefile
from ugv_nav_tools.map_files import (
    inside,
    keepout_pixels,
    load_grid,
    read_map_yaml,
    read_pgm,
    sha256_file,
    write_keepout_mask,
    write_pgm,
)
from ugv_nav_tools.navctl import NavCtl, Settings, main

SIZE = 20  # 1 m x 1 m at 5 cm


def write_map(maps: Path, name: str = "home1", wall: bool = True) -> Path:
    """A square room: occupied border, free inside, one unknown cell."""
    pixels = bytearray([254]) * (SIZE * SIZE)
    if wall:
        for i in range(SIZE):
            for row, column in ((0, i), (SIZE - 1, i), (i, 0), (i, SIZE - 1)):
                pixels[row * SIZE + column] = 0
    pixels[5 * SIZE + 5] = 205
    write_pgm(maps / f"{name}.pgm", SIZE, SIZE, bytes(pixels))
    yaml = maps / f"{name}.yaml"
    yaml.write_text(
        f"image: {name}.pgm\nmode: trinary\nresolution: 0.05\norigin: [-0.5, -0.5, 0]\n"
        "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n"
    )
    return yaml


def test_map_yaml_pgm_and_grid_orientation(tmp_path: Path) -> None:
    yaml = write_map(tmp_path)
    info = read_map_yaml(yaml)
    assert (info.resolution, info.origin_x, info.origin_y, info.negate) == (0.05, -0.5, -0.5, False)
    grid = load_grid(yaml)
    assert (grid.width, grid.height) == (SIZE, SIZE)
    assert grid.data[0] == 100  # bottom-left wall
    assert grid.data[10 * SIZE + 10] == 0  # free middle
    # PGM row 5 (top-down) is grid row SIZE-1-5 (bottom-up): the unknown cell.
    assert grid.data[(SIZE - 1 - 5) * SIZE + 5] == -1

    bad = tmp_path / "bad.yaml"
    bad.write_text("not yaml at all\n")
    with pytest.raises(ValueError):
        read_map_yaml(bad)
    bad.write_text("image: ../x.pgm\nresolution: 0.05\norigin: [0, 0, 0]\n")
    with pytest.raises(ValueError):
        read_map_yaml(bad)
    ascii_pgm = tmp_path / "ascii.pgm"
    ascii_pgm.write_bytes(b"P2\n1 1\n255\n0\n")
    with pytest.raises(ValueError, match="P5"):
        read_pgm(ascii_pgm)
    commented = tmp_path / "commented.pgm"
    commented.write_bytes(b"P5\n# map_saver\n2 1\n255\n\x00\xfe")
    assert read_pgm(commented) == (2, 1, b"\x00\xfe")


def test_keepout_mask_marks_cells_inside_the_polygon(tmp_path: Path) -> None:
    square = [(0.0, 0.0), (0.2, 0.0), (0.2, 0.2), (0.0, 0.2)]
    assert inside(0.1, 0.1, square) and not inside(0.3, 0.1, square)
    pixels = keepout_pixels(SIZE, SIZE, 0.05, -0.5, -0.5, [square])
    assert pixels.count(0) == 16  # 4 x 4 cells of 5 cm
    # Cell (col 10, row 10) holds the point (0.025, 0.025): top-down row SIZE-1-10.
    assert pixels[(SIZE - 1 - 10) * SIZE + 10] == 0
    yaml = write_map(tmp_path)
    mask, cells = write_keepout_mask(yaml, [square])
    assert cells == 16 and mask.name == "home1.keepout.yaml"
    assert "image: home1.keepout.pgm" in mask.read_text()
    assert read_pgm(tmp_path / "home1.keepout.pgm")[2].count(0) == 16
    assert os.stat(mask).st_mode & 0o777 == 0o600


def sample(sha: str = "a" * 64) -> dict[str, Any]:
    return {
        "version": 1,
        "map": "home1",
        "map_sha256": sha,
        "rooms": [{"id": "living-room", "name": "客厅", "aliases": ["大厅"]}],
        "places": [
            {"id": "start", "name": "出发点", "kind": "home", "pose": {"x": 0, "y": 0, "yaw": 0}},
            {
                "id": "sofa",
                "name": "沙发前",
                "kind": "observe",
                "room": "living-room",
                "scan": [-90, 0, 90],
                "pose": {"x": 0.1, "y": 0.0, "yaw": 1.57},
            },
        ],
        "zones": [{"id": "balcony", "name": "阳台", "polygon": [[0, 0], [1, 0], [1, 1]]}],
    }


def test_places_parse_resolve_and_round_trip() -> None:
    places = placefile.parse(sample())
    assert places.resolve("大厅") == ("room", "living-room")
    assert places.resolve("沙发前") == ("place", "sofa")
    assert places.resolve("阳台") == ("zone", "balcony")
    assert places.resolve("卫生间") is None
    assert [p.id for p in places.observe_points("living-room")] == ["sofa"]
    home = places.home()
    assert home is not None and home.id == "start"
    assert places.place("sofa").scan == (-90, 0, 90)
    with pytest.raises(KeyError):
        places.place("nowhere")
    assert placefile.parse(places.to_json()) == places


@pytest.mark.parametrize(
    "change, message",
    [
        ({"version": 2}, "version"),
        ({"map_sha256": "xyz"}, "sha256"),
        ({"rooms": [{"id": "Living Room", "name": "客厅"}]}, "id"),
        (
            {
                "rooms": [
                    {"id": "living-room", "name": "客厅", "aliases": ["大厅"]},
                    {"id": "hall", "name": "大厅"},
                ]
            },
            "twice",
        ),
        ({"rooms": "客厅"}, "lists"),
        ({"zones": [{"id": "z", "name": "z", "polygon": [[0, 0], [1, 1]]}]}, "3-32"),
        ({"zones": [{"id": "z", "name": "z", "kind": "slow", "polygon": []}]}, "keepout"),
    ],
)
def test_places_reject_bad_files(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        placefile.parse(sample() | change)


@pytest.mark.parametrize(
    "place, message",
    [
        ({"room": "attic"}, "unknown room"),
        ({"scan": [200]}, "-180..180"),
        ({"scan": [0.5]}, "-180..180"),
        ({"kind": "home"}, "one home"),
        ({"kind": "charger"}, "kind"),
        ({"pose": {"x": float("nan"), "y": 0, "yaw": 0}}, "finite"),
        ({"id": "start"}, "duplicate"),
    ],
)
def test_places_reject_bad_places(place: dict[str, Any], message: str) -> None:
    value = sample()
    value["places"][1] |= place
    with pytest.raises(ValueError, match=message):
        placefile.parse(value)


def test_save_load_and_map_change(tmp_path: Path) -> None:
    write_map(tmp_path)
    places = placefile.parse(sample(sha256_file(tmp_path / "home1.pgm")))
    path = placefile.save(tmp_path, places)
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert placefile.load(tmp_path, "home1") == places
    write_map(tmp_path, wall=False)  # the map was rebuilt
    with pytest.raises(ValueError, match="mark them again"):
        placefile.load(tmp_path, "home1")


def test_places_need_free_space_to_turn(tmp_path: Path) -> None:
    grid = load_grid(write_map(tmp_path))
    good = placefile.parse(sample()).place("sofa")  # (0.1, 0): 0.6 m from every wall
    placefile.check_place(good, grid)
    near_wall = placefile.Place("wall", "墙边", "observe", 0.4, 0.4, 0.0)
    with pytest.raises(ValueError, match="free space"):
        placefile.check_place(near_wall, grid)


class Runner:
    """docker exec stand-in: answers ugv-ros-op pose and navigate."""

    def __init__(self) -> None:
        self.navigated: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        stdout = ""
        if "ugv-ros-op" in command:
            op = command[command.index("ugv-ros-op") + 1]
            if op == "pose":
                stdout = json.dumps({"x": 0.1, "y": -0.1, "yaw": 0.3})
            elif op == "navigate":
                self.navigated.append(command)
                stdout = json.dumps({"ok": True})
            else:
                stdout = "{}"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def test_navctl_marks_places_zones_and_goes_to_a_place(tmp_path: Path) -> None:
    (tmp_path / "ugv_lidar").write_text("")
    settings = Settings(
        deploy=tmp_path / "deploy", board=tmp_path / "board", data=tmp_path / "data",
        lidar_device=tmp_path / "ugv_lidar",
    )  # fmt: skip
    for folder in (settings.board, settings.compose_dir, settings.maps):
        folder.mkdir(parents=True)
    write_map(settings.maps)
    runner = Runner()
    board = Mock()
    board.status.return_value = {"ok": True, "telemetry": {"L": 0.0, "R": 0.0, "v": 12.0}}
    printed: list[str] = []
    ctl = NavCtl(settings, run=runner, board=board, echo=printed.append)

    assert main(["place", "home1", "room", "living-room", "客厅", "--alias", "大厅"], ctl) == 0
    args = ["place", "home1", "add", "sofa", "沙发前", "--room", "living-room"]
    assert main([*args, "--scan=-90,0,90", "--at", "0.1", "0", "1.57"], ctl) == 0
    assert main(["place", "home1", "add", "start", "出发点", "--kind", "home", "--here"], ctl) == 0
    with pytest.raises(ValueError, match="free space"):
        main(["place", "home1", "add", "corner", "墙角", "--at", "0.4", "0.4", "0"], ctl)
    with pytest.raises(ValueError, match="unknown room"):
        main(
            ["place", "home1", "add", "bed", "床边", "--room", "bedroom", "--at", "0", "0", "0"],
            ctl,
        )
    zone = ["place", "home1", "zone", "balcony", "阳台", "0.2,0.2", "0.4,0.2", "0.4,0.4"]
    assert main(zone, ctl) == 0
    assert main(["place", "home1", "apply-zones"], ctl) == 0
    assert json.loads(printed[-1])["keepout_cells"] > 0
    ctl.set_mode(mapping=False, name="home1")
    env_text = (settings.compose_dir / ".env").read_text()
    assert "map:=/maps/home1.yaml keepout:=/maps/home1.keepout.yaml" in env_text
    assert main(["place", "home1", "list"], ctl) == 0
    listed = json.loads(printed[-1])
    assert [p["id"] for p in listed["places"]] == ["sofa", "start"]
    assert listed["places"][1]["pose"] == {"x": 0.1, "y": -0.1, "yaw": 0.3}
    with pytest.raises(ValueError, match="still has places"):
        main(["place", "home1", "rm", "living-room"], ctl)
    with pytest.raises(ValueError, match="no room"):
        main(["place", "home1", "rm", "garage"], ctl)
    assert main(["place", "home1", "rm", "balcony"], ctl) == 0

    (settings.compose_dir / ".env").write_text("UGV_NAV_ARGS=mapping:=true localization:=false\n")
    with pytest.raises(ValueError, match="localize"):
        ctl.goto("sofa")
    env = "UGV_NAV_ARGS=mapping:=false localization:=true navigation:=true map:=/maps/home1.yaml\n"
    (settings.compose_dir / ".env").write_text(env)
    assert main(["goto", "sofa"], ctl) == 0
    assert runner.navigated[-1][-5:-2] == ["0.1", "0.0", "1.57"]
    assert json.loads(printed[-1])["place"] == "sofa"
    with pytest.raises(KeyError):
        ctl.goto("garage")
