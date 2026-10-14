"""Replaceable media interfaces for the later Raspberry Pi hardware phase."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class VideoFrame:
    timestamp_ns: int
    width: int
    height: int
    encoding: str
    data: bytes


@dataclass(frozen=True)
class AudioChunk:
    timestamp_ns: int
    sample_rate_hz: int
    channels: int
    encoding: str
    data: bytes


class CameraPort(Protocol):
    """Camera adapter; only called while a parent-authorized session is active."""

    def open(self) -> None: ...
    def read(self) -> VideoFrame: ...
    def close(self) -> None: ...


class MicrophonePort(Protocol):
    def open(self) -> None: ...
    def read(self) -> AudioChunk: ...
    def close(self) -> None: ...


class SpeakerPort(Protocol):
    def open(self) -> None: ...
    def play(self, chunk: AudioChunk) -> None: ...
    def close(self) -> None: ...
