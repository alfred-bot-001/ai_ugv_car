"""Explicit session lifecycle; no default adapter or autonomous capture."""

from dataclasses import dataclass
from enum import StrEnum

from .media import AudioChunk, CameraPort, MicrophonePort, SpeakerPort, VideoFrame

PROTOCOL_VERSION = 1


class SessionState(StrEnum):
    IDLE = "idle"
    ACTIVE = "active"


@dataclass(frozen=True)
class SessionRequest:
    session_id: str
    video: bool = False
    microphone: bool = False
    speaker: bool = False
    version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.version != PROTOCOL_VERSION:
            raise ValueError("unsupported protocol version")
        if not self.session_id or len(self.session_id) > 128:
            raise ValueError("session_id must be 1..128 characters")
        if not any((self.video, self.microphone, self.speaker)):
            raise ValueError("session must enable at least one media port")


class PiSessionController:
    def __init__(
        self,
        camera: CameraPort | None = None,
        microphone: MicrophonePort | None = None,
        speaker: SpeakerPort | None = None,
    ) -> None:
        self.camera = camera
        self.microphone = microphone
        self.speaker = speaker
        self.state = SessionState.IDLE
        self.request: SessionRequest | None = None

    def capabilities(self) -> dict[str, object]:
        return {
            "version": PROTOCOL_VERSION,
            "video": self.camera is not None,
            "microphone": self.microphone is not None,
            "speaker": self.speaker is not None,
            "sessionState": self.state.value,
        }

    def start(self, request: SessionRequest) -> None:
        if self.state != SessionState.IDLE:
            raise RuntimeError("another session is active")
        required = (
            (request.video, self.camera, "camera"),
            (request.microphone, self.microphone, "microphone"),
            (request.speaker, self.speaker, "speaker"),
        )
        missing = [name for enabled, adapter, name in required if enabled and adapter is None]
        if missing:
            raise RuntimeError("media adapter unavailable: " + ", ".join(missing))
        opened = []
        try:
            for enabled, adapter, _ in required:
                if enabled and adapter is not None:
                    adapter.open()
                    opened.append(adapter)
        except Exception:
            for adapter in reversed(opened):
                try:
                    adapter.close()
                except Exception:
                    pass
            raise
        self.request = request
        self.state = SessionState.ACTIVE

    def read_video(self) -> VideoFrame:
        if self.state != SessionState.ACTIVE or not self.request or not self.request.video:
            raise RuntimeError("video session is not active")
        assert self.camera is not None
        return self.camera.read()

    def read_microphone(self) -> AudioChunk:
        if self.state != SessionState.ACTIVE or not self.request or not self.request.microphone:
            raise RuntimeError("microphone session is not active")
        assert self.microphone is not None
        return self.microphone.read()

    def play(self, chunk: AudioChunk) -> None:
        if self.state != SessionState.ACTIVE or not self.request or not self.request.speaker:
            raise RuntimeError("speaker session is not active")
        assert self.speaker is not None
        self.speaker.play(chunk)

    def stop(self, session_id: str) -> None:
        if self.state == SessionState.IDLE:
            return
        if not self.request or self.request.session_id != session_id:
            raise ValueError("session_id mismatch")
        first_error = None
        for enabled, adapter in (
            (self.request.speaker, self.speaker),
            (self.request.microphone, self.microphone),
            (self.request.video, self.camera),
        ):
            if enabled and adapter is not None:
                try:
                    adapter.close()
                except Exception as error:
                    if first_error is None:
                        first_error = error
        self.request = None
        self.state = SessionState.IDLE
        if first_error is not None:
            raise first_error
