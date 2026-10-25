"""Framing between tvpet_pi.depth_worker and the agent: a header, JSON metadata, a JPEG.

Standard library only: the worker runs in pose-venv, where the agent's own dependencies
(pydantic and the rest) are not installed, so nothing here may import them.
"""

import json
import struct
from typing import IO, Any

MAX_META = 4096
MAX_JPEG = 1_048_576  # same cap as protocol.MAX_JPEG (test_depth checks)
HEADER = struct.Struct("!II")  # metadata length, JPEG length
# Colour scale of the depth image (mm): blue near, through green and yellow, red far.
NEAR_MM = 300
FAR_MM = 4000


def encode_frame(meta: dict[str, Any], jpeg: bytes) -> bytes:
    data = json.dumps(meta, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > MAX_META or not jpeg.startswith(b"\xff\xd8") or len(jpeg) > MAX_JPEG:
        raise ValueError("invalid depth frame")
    return HEADER.pack(len(data), len(jpeg)) + data + jpeg


def read_exact(stream: IO[bytes], size: int) -> bytes:
    data = b""
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            raise EOFError("depth worker closed its output")
        data += part
    return data


def read_frame(stream: IO[bytes]) -> tuple[dict[str, Any], bytes]:
    """One frame from the worker; malformed output raises ValueError."""
    meta_size, jpeg_size = HEADER.unpack(read_exact(stream, HEADER.size))
    if not 1 <= meta_size <= MAX_META or not 2 <= jpeg_size <= MAX_JPEG:
        raise ValueError("invalid depth frame header")
    meta = json.loads(read_exact(stream, meta_size))
    jpeg = read_exact(stream, jpeg_size)
    if not isinstance(meta, dict) or not isinstance(meta.get("sequence"), int):
        raise ValueError("invalid depth frame metadata")
    if not jpeg.startswith(b"\xff\xd8"):
        raise ValueError("invalid depth JPEG")
    return meta, jpeg
