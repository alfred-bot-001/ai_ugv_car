"""Places file (S1 §4.1): rooms, named places and keepout zones bound to one saved map.

`maps/<map>.places.json`, 0600. Programs and AI_Home refer to rooms and places by id
only; names and aliases are what people say. Pure logic, no ROS.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .map_files import Grid, atomic_write, sha256_file
from .motion_gate import rotation_space_clear

ID = re.compile(r"[a-z0-9-]{1,32}")
SHA256 = re.compile(r"[0-9a-f]{64}")
PLACE_KINDS = ("home", "observe", "waypoint")
MAX_ROOMS = 20
MAX_PLACES = 50
MAX_ZONES = 20
MAX_ALIASES = 8
MAX_SCAN = 9
NAME_MAX = 32


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Place:
    id: str
    name: str
    kind: str
    x: float
    y: float
    yaw: float
    aliases: tuple[str, ...] = ()
    room: str | None = None
    # Gimbal pan angles in degrees to look at when observing; empty means the default.
    scan: tuple[int, ...] = ()


@dataclass(frozen=True)
class Zone:
    id: str
    name: str
    polygon: tuple[tuple[float, float], ...]
    kind: str = "keepout"


@dataclass(frozen=True)
class Places:
    map: str
    map_sha256: str
    rooms: tuple[Room, ...] = ()
    places: tuple[Place, ...] = ()
    zones: tuple[Zone, ...] = field(default_factory=tuple)

    def place(self, place_id: str) -> Place:
        for place in self.places:
            if place.id == place_id:
                return place
        raise KeyError(place_id)

    def home(self) -> Place | None:
        return next((p for p in self.places if p.kind == "home"), None)

    def observe_points(self, room_id: str) -> list[Place]:
        """Places to look from inside a room, in file order."""
        return [p for p in self.places if p.room == room_id and p.kind == "observe"]

    def resolve(self, word: str) -> tuple[str, str] | None:
        """("room" | "place" | "zone", id) for an id, name or alias; None if unknown."""
        word = word.strip()
        for kind, items in (("room", self.rooms), ("place", self.places), ("zone", self.zones)):
            for item in items:
                aliases = getattr(item, "aliases", ())
                if word in (item.id, item.name, *aliases):
                    return kind, item.id
        return None

    def to_json(self) -> dict[str, Any]:
        def place(p: Place) -> dict[str, Any]:
            value: dict[str, Any] = {
                "id": p.id,
                "name": p.name,
                "aliases": list(p.aliases),
                "kind": p.kind,
                "pose": {"x": p.x, "y": p.y, "yaw": p.yaw},
            }
            if p.room:
                value["room"] = p.room
            if p.scan:
                value["scan"] = list(p.scan)
            return value

        return {
            "version": 1,
            "map": self.map,
            "map_sha256": self.map_sha256,
            "rooms": [{"id": r.id, "name": r.name, "aliases": list(r.aliases)} for r in self.rooms],
            "places": [place(p) for p in self.places],
            "zones": [
                {
                    "id": z.id,
                    "name": z.name,
                    "kind": z.kind,
                    "polygon": [list(v) for v in z.polygon],
                }
                for z in self.zones
            ],
        }


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > NAME_MAX:
        raise ValueError(f"{what} must be 1-{NAME_MAX} characters")
    return value.strip()


def _id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError(f"{what} id must match [a-z0-9-]{{1,32}}")
    return value


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} must be a finite number")
    return float(value)


def _aliases(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_ALIASES:
        raise ValueError(f"{what} aliases must be a list of at most {MAX_ALIASES}")
    return tuple(_text(a, f"{what} alias") for a in value)


def parse(value: Any) -> Places:
    """Validate a decoded places file; raises ValueError with the first problem."""
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("places file must be version 1")
    map_name = _text(value.get("map"), "map name")
    sha = value.get("map_sha256")
    if not isinstance(sha, str) or not SHA256.fullmatch(sha):
        raise ValueError("map_sha256 must be a sha256 hex digest")
    rooms_raw, places_raw, zones_raw = (value.get(k, []) for k in ("rooms", "places", "zones"))
    if not all(isinstance(v, list) for v in (rooms_raw, places_raw, zones_raw)):
        raise ValueError("rooms, places and zones must be lists")
    if len(rooms_raw) > MAX_ROOMS or len(places_raw) > MAX_PLACES or len(zones_raw) > MAX_ZONES:
        raise ValueError(f"at most {MAX_ROOMS} rooms, {MAX_PLACES} places, {MAX_ZONES} zones")

    rooms = []
    for raw in rooms_raw:
        if not isinstance(raw, dict):
            raise ValueError("room must be an object")
        rid = _id(raw.get("id"), "room")
        rooms.append(
            Room(rid, _text(raw.get("name"), "room name"), _aliases(raw.get("aliases"), rid))
        )
    room_ids = {r.id for r in rooms}

    places = []
    for raw in places_raw:
        if not isinstance(raw, dict) or not isinstance(raw.get("pose"), dict):
            raise ValueError("place must be an object with a pose")
        pid = _id(raw.get("id"), "place")
        kind = raw.get("kind", "observe")
        if kind not in PLACE_KINDS:
            raise ValueError(f"place {pid} kind must be one of {', '.join(PLACE_KINDS)}")
        room = raw.get("room")
        if room is not None and room not in room_ids:
            raise ValueError(f"place {pid} refers to unknown room {room!r}")
        scan_raw = raw.get("scan", [])
        if not isinstance(scan_raw, list) or len(scan_raw) > MAX_SCAN:
            raise ValueError(f"place {pid} scan must list at most {MAX_SCAN} angles")
        scan = []
        for angle in scan_raw:
            if isinstance(angle, bool) or not isinstance(angle, int) or not -180 <= angle <= 180:
                raise ValueError(f"place {pid} scan angles must be integers in -180..180")
            scan.append(angle)
        pose = raw["pose"]
        places.append(
            Place(
                pid,
                _text(raw.get("name"), "place name"),
                kind,
                _number(pose.get("x"), f"{pid} x"),
                _number(pose.get("y"), f"{pid} y"),
                _number(pose.get("yaw"), f"{pid} yaw"),
                _aliases(raw.get("aliases"), pid),
                room,
                tuple(scan),
            )
        )
    if sum(p.kind == "home" for p in places) > 1:
        raise ValueError("at most one home place")

    zones = []
    for raw in zones_raw:
        if not isinstance(raw, dict) or raw.get("kind", "keepout") != "keepout":
            raise ValueError("zone must be a keepout object")
        zid = _id(raw.get("id"), "zone")
        polygon = raw.get("polygon")
        if not isinstance(polygon, list) or not 3 <= len(polygon) <= 32:
            raise ValueError(f"zone {zid} polygon needs 3-32 vertices")
        vertices = []
        for vertex in polygon:
            if not isinstance(vertex, list) or len(vertex) != 2:
                raise ValueError(f"zone {zid} vertices are [x, y]")
            vertices.append((_number(vertex[0], f"{zid} x"), _number(vertex[1], f"{zid} y")))
        zones.append(Zone(zid, _text(raw.get("name"), "zone name"), tuple(vertices)))

    entries = [(r.id, (r.name, *r.aliases)) for r in rooms]
    entries += [(p.id, (p.name, *p.aliases)) for p in places]
    entries += [(z.id, (z.name,)) for z in zones]
    seen_ids: set[str] = set()
    seen_words: set[str] = set()
    for item_id, words in entries:
        if item_id in seen_ids:
            raise ValueError(f"duplicate id {item_id}")
        seen_ids.add(item_id)
        for word in words:
            if word in seen_words:
                raise ValueError(f"name or alias {word!r} is used twice")
            seen_words.add(word)
    return Places(map_name, sha, tuple(rooms), tuple(places), tuple(zones))


def places_path(maps: Path, map_name: str) -> Path:
    return maps / f"{map_name}.places.json"


def load(maps: Path, map_name: str) -> Places:
    """Load and check that the places still belong to the saved map's current image."""
    places = parse(json.loads(places_path(maps, map_name).read_text()))
    if places.map != map_name:
        raise ValueError("places file names a different map")
    if sha256_file(maps / f"{map_name}.pgm") != places.map_sha256:
        raise ValueError("map image changed since the places were marked; mark them again")
    return places


def load_or_new(maps: Path, map_name: str) -> Places:
    if places_path(maps, map_name).exists():
        return load(maps, map_name)
    return Places(map_name, sha256_file(maps / f"{map_name}.pgm"))


def save(maps: Path, places: Places) -> Path:
    path = places_path(maps, places.map)
    parse(places.to_json())  # never write something load() would reject
    atomic_write(path, (json.dumps(places.to_json(), ensure_ascii=False, indent=1) + "\n").encode())
    return path


def check_place(place: Place, grid: Grid) -> None:
    """A place must be inside the map with known free space to turn and look around."""
    if not rotation_space_clear(
        grid.data,
        grid.width,
        grid.height,
        grid.resolution,
        grid.origin_x,
        grid.origin_y,
        place.x,
        place.y,
    ):
        raise ValueError(f"place {place.id} has no verified free space to turn in")
