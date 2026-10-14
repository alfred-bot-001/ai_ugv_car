"""Bounded, transient video recognition through the Mini's existing local service."""

import asyncio
import json
import struct
import time
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator

from .pose_review import ReviewRecorder, associate

LABELS = {"max": "爸爸", "son": "儿子", "wife": "妈妈"}
Coordinate = Annotated[float, Field(allow_inf_nan=False, ge=-8192, le=8192)]


class Face(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    bbox: tuple[Coordinate, Coordinate, Coordinate, Coordinate]

    @field_validator("bbox")
    @classmethod
    def ordered_box(cls, box: tuple[float, float, float, float]) -> tuple[float, ...]:
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("invalid face box")
        return box


class Recognition(BaseModel):
    faces: list[Face] = Field(max_length=16)
    time_ms: float = Field(ge=0, le=60000, allow_inf_nan=False)

    def boxes(self) -> list[dict[str, Any]]:
        """Return only display data; exclude other biometric attributes."""
        return [
            {
                "bbox": face.bbox,
                "label": LABELS.get(face.name, face.name)
                if face.name != "unknown" and face.confidence >= 0.4
                else "未登记",
                "known": face.name != "unknown" and face.confidence >= 0.4,
                "similarity": face.confidence,
            }
            for face in self.faces
        ]


def validate_service_url(url: str) -> str:
    """Restrict the server-side adapter to a configured local service."""
    if not url:
        return ""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("人脸服务必须是 Mini 本机的 loopback URL")
    return url.rstrip("/")


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    sequence: int
    received_at: float
    generation: int
    pose: dict[str, Any] = field(default_factory=dict)
    timestamp_ns: int = 0


class FaceVideo:
    def __init__(self, url: str = "", *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = validate_service_url(url)
        self.transport = transport
        self.active = False
        self.generation = 0
        self.latest: Frame | None = None
        self.input_event = asyncio.Event()
        self.output_event = asyncio.Condition()
        self.packet = b""
        self.version = 0
        self.state = "idle" if self.url else "disabled"
        self.inference_ms: float | None = None
        self.retry_at = 0.0
        self.recorder = ReviewRecorder()

    def reset(self, active: bool = False) -> None:
        """Invalidate any in-flight result when a media session ends."""
        self.generation += 1
        self.active = active
        self.latest = None
        self.packet = b""
        self.input_event.clear()
        self.state = "waiting" if active and self.url else "idle" if self.url else "disabled"
        self.inference_ms = None
        self.retry_at = 0.0

    def submit(
        self, jpeg: bytes, sequence: int, pose: dict[str, Any] | None = None, timestamp_ns: int = 0
    ) -> None:
        if self.active:
            self.latest = Frame(
                jpeg, sequence, time.monotonic(), self.generation, pose or {}, timestamp_ns
            )
            self.input_event.set()

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "inference_ms": self.inference_ms}

    async def publish(
        self, frame: Frame, state: str, faces: list[dict[str, Any]], inference_ms: float | None
    ) -> None:
        if not self.active or frame.generation != self.generation:
            return
        if time.monotonic() - frame.received_at > 1.5:
            return
        metadata = json.dumps(
            {
                "sequence": frame.sequence,
                "recognition": state,
                "faces": faces,
                "inference_ms": inference_ms,
                "pose": associate(frame.pose, faces),
                "timestamp_ns": frame.timestamp_ns,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        async with self.output_event:
            self.packet = struct.pack("!I", len(metadata)) + metadata + frame.jpeg
            self.version += 1
            self.output_event.notify_all()
            await self.recorder.consume(self.packet)

    async def run(self) -> None:
        """Share one inference request among viewers, always dropping queued old frames."""
        async with httpx.AsyncClient(
            timeout=1, trust_env=False, transport=self.transport, follow_redirects=False
        ) as client:
            while True:
                await self.input_event.wait()
                self.input_event.clear()
                frame = self.latest
                if frame is None or not self.active:
                    continue
                if not self.url or time.monotonic() < self.retry_at:
                    await self.publish(frame, self.state, [], None)
                    continue
                start = time.monotonic()
                try:
                    response = await asyncio.wait_for(
                        client.post(
                            self.url + "/recognize",
                            files={"file": ("frame.jpg", frame.jpeg, "image/jpeg")},
                        ),
                        timeout=1,
                    )
                    response.raise_for_status()
                    result = Recognition.model_validate(response.json())
                    state, boxes, duration = "active", result.boxes(), result.time_ms
                except (httpx.HTTPError, ValueError, TimeoutError):
                    state, boxes, duration = "unavailable", [], None
                if self.active and frame.generation == self.generation:
                    self.state, self.inference_ms = state, duration
                    if state == "unavailable":
                        self.retry_at = time.monotonic() + 3
                        # Failed recognition must not hold back the latest camera view.
                        frame = self.latest or frame
                    await self.publish(frame, state, boxes, duration)
                await asyncio.sleep(max(0, 0.2 - (time.monotonic() - start)))
