"""Front Orbbec color camera: optional, leased like the gimbal camera, frames only."""

import asyncio
import contextlib
import json
import struct
import time
from pathlib import Path

import pytest
from tvpet_pi.agent import AgentRuntime
from tvpet_pi.capture import Frame, mjpeg_payload

FRONT = "/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._USB_2.0_Camera_AY27552002R-video-index0"


class Worker:
    def __init__(self, value=None):
        self.active = False
        self.error = ""
        self.value = value
        self.renewed = 0

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def renew(self):
        self.renewed += 1

    def latest(self):
        return 1, self.value


class Socket:
    def __init__(self, messages=()):
        self.sent = []
        self.messages = iter(messages)

    async def send(self, message):
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.messages)
        except StopIteration:
            raise StopAsyncIteration from None


def test_front_camera_is_optional_and_leased() -> None:
    async def scenario():
        runtime = AgentRuntime({})
        assert runtime.front_camera is None
        runtime.control = Socket()
        await runtime.command(
            {"id": "a", "action": "media.set", "args": {"channel": "front", "enabled": True}}
        )
        assert json.loads(runtime.control.sent[-1])["error"] == "front camera is not configured"

        runtime = AgentRuntime({"front_camera": FRONT})
        assert runtime.front_camera is not None and runtime.front_camera.adapter.path == FRONT
        worker = Worker()
        runtime.front_camera = worker
        runtime.control = Socket()
        await runtime.command(
            {"id": "b", "action": "media.set", "args": {"channel": "front", "enabled": True}}
        )
        assert json.loads(runtime.control.sent[-1])["ok"] and worker.active
        runtime.control = Socket([json.dumps({"kind": "heartbeat", "front": True})])
        await runtime._control_reader()
        assert worker.renewed == 1
        runtime.control = Socket()
        await runtime.command(
            {"id": "c", "action": "media.set", "args": {"channel": "front", "enabled": False}}
        )
        assert not worker.active and not runtime.front_enabled

    asyncio.run(scenario())


def test_front_frames_skip_pose_inference() -> None:
    async def scenario():
        runtime = AgentRuntime({"front_camera": FRONT})
        runtime.front_enabled = True
        runtime.front_camera = Worker(Frame(7, time.monotonic_ns(), 0x2000, b"\xff\xd8f\xff\xd9"))

        async def no_inference(jpeg):
            raise AssertionError("the front camera must not run pose inference")

        runtime.pose.infer = no_inference
        socket = Socket()
        task = asyncio.create_task(runtime._capture_sender(socket, "front", None))
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        packet = socket.sent[0]
        length = struct.unpack_from("!I", packet)[0]
        assert json.loads(packet[4 : 4 + length])["sequence"] == 7
        assert packet[4 + length :] == b"\xff\xd8f\xff\xd9"

    asyncio.run(scenario())


def test_front_status_and_channel_retry(tmp_path: Path, monkeypatch) -> None:
    runtime = AgentRuntime({"front_camera": str(tmp_path / "video9")})
    runtime.board.status = lambda: {"wheels_ready": False, "gimbal_ready": False}
    monkeypatch.setattr("tvpet_pi.agent.Path.read_text", lambda self: "")
    monkeypatch.setattr("tvpet_pi.agent.system_status", lambda: {})
    assert runtime.status()["front_camera"]["state"] == "unknown"
    (tmp_path / "video9").touch()
    assert runtime.status()["front_camera"]["state"] == "available"
    assert AgentRuntime({}).status()["front_camera"]["state"] == "disabled"

    def refuse(*args, **kwargs):
        raise OSError("refused")

    monkeypatch.setattr("tvpet_pi.agent.connect", refuse)

    async def scenario():
        task = asyncio.create_task(runtime._front_channel("link", None, "token"))
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_mjpeg_padding_is_trimmed_but_garbage_is_not() -> None:
    jpeg = b"\xff\xd8\xff\xc0body\xff\xd9"
    assert mjpeg_payload(jpeg) == jpeg
    assert mjpeg_payload(jpeg + b"\x00" * 5) == jpeg  # Orbbec pads to 8 bytes
    for bad in (
        b"\x00" + jpeg,
        jpeg + b"\x01\x00",
        jpeg + b"\x00" * 80,
        b"\xff\xd8truncated",
    ):
        with pytest.raises(ValueError):
            mjpeg_payload(bad)
