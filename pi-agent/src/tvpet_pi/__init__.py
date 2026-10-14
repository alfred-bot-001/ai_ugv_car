"""Raspberry Pi camera, microphone, and speaker contracts.

M1 ships protocol boundaries only. No hardware is opened until an explicit
future session request and hardware adapter implementation are supplied.
"""

from .media import AudioChunk, CameraPort, MicrophonePort, SpeakerPort, VideoFrame
from .session import PiSessionController, SessionRequest, SessionState

__all__ = [
    "AudioChunk",
    "CameraPort",
    "MicrophonePort",
    "SpeakerPort",
    "VideoFrame",
    "PiSessionController",
    "SessionRequest",
    "SessionState",
]
