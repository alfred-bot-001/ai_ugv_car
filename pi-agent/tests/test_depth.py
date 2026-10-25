"""Front Orbbec depth: worker framing, the child-process adapter and the depth channel."""

import asyncio
import io
import json
import struct
import sys
import time
from pathlib import Path

import pytest
from tvpet_pi.agent import AgentRuntime
from tvpet_pi.depth import HEADER, DepthCamera, DepthFrame, encode_frame, read_frame

JPEG = b"\xff\xd8depth\xff\xd9"


def test_frames_round_trip_and_bad_output_is_rejected() -> None:
    meta = {"sequence": 3, "centre_mm": 1234, "valid": 0.62}
    assert read_frame(io.BytesIO(encode_frame(meta, JPEG))) == (meta, JPEG)
    with pytest.raises(ValueError):
        encode_frame(meta, b"not a jpeg")
    with pytest.raises(ValueError):
        encode_frame({"pad": "x" * 5000}, JPEG)
    for bad in (
        HEADER.pack(0, 5) + b"jpeg!",
        HEADER.pack(5000, 5),
        HEADER.pack(2, 4) + b"{}" + JPEG[:4],  # no sequence
        HEADER.pack(14, 4) + b'{"sequence":1}' + b"abcd",  # not a JPEG
    ):
        with pytest.raises(ValueError):
            read_frame(io.BytesIO(bad))
    with pytest.raises(EOFError):
        read_frame(io.BytesIO(HEADER.pack(14, 9) + b'{"sequence":1}'))


FAKE_WORKER = """
import sys, time
from tvpet_pi.depth import encode_frame
for n in (1, 2):
    sys.stdout.buffer.write(encode_frame({"sequence": n, "centre_mm": 800 + n}, b"\\xff\\xd8x"))
    sys.stdout.buffer.flush()
sys.stdin.buffer.read()  # like the real worker: exit when the agent closes stdin
"""


def test_camera_reads_the_worker_and_stops_it() -> None:
    camera = DepthCamera("python", "/openni", command=[sys.executable, "-c", FAKE_WORKER])
    assert DepthCamera("/py", "/o").command[:3] == ["/py", "-m", "tvpet_pi.depth_worker"]
    camera.open()
    first, second = camera.read(), camera.read()
    assert isinstance(first, DepthFrame) and first.sequence == 1 and second.meta["centre_mm"] == 802
    process = camera.process
    started = time.monotonic()
    camera.close()
    assert process is not None and process.returncode == 0 and time.monotonic() - started < 2
    with pytest.raises(OSError):
        camera.read()
    camera.open()
    camera.command = []  # a worker that dies says so
    camera.close()
    dying = DepthCamera("python", "/openni", command=[sys.executable, "-c", "pass"])
    dying.open()
    with pytest.raises(OSError, match="stopped"):
        dying.read()
    dying.close()


class Worker:
    def __init__(self, value: object = None) -> None:
        self.active = False
        self.error = ""
        self.value = value
        self.renewed = 0

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def renew(self) -> None:
        self.renewed += 1

    def latest(self) -> tuple[int, object]:
        return 1, self.value


class Socket:
    def __init__(self, messages: tuple[str, ...] = ()) -> None:
        self.sent: list[object] = []
        self.messages = iter(messages)

    async def send(self, message: object) -> None:
        self.sent.append(message)

    def __aiter__(self) -> "Socket":
        return self

    async def __anext__(self) -> str:
        try:
            return next(self.messages)
        except StopIteration:
            raise StopAsyncIteration from None


def test_depth_channel_is_optional_leased_and_carries_distances(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = AgentRuntime({})
        assert runtime.depth_camera is None
        runtime.control = Socket()  # type: ignore[assignment]
        message = {"id": "a", "action": "media.set", "args": {"channel": "depth", "enabled": True}}
        await runtime.command(message)
        assert json.loads(runtime.control.sent[-1])["error"] == "depth camera is not configured"  # type: ignore[union-attr, arg-type]

        runtime = AgentRuntime({"depth_openni": str(tmp_path), "pose_python": "/py"})
        assert runtime.depth_camera is not None
        worker = Worker(DepthFrame(5, time.monotonic_ns(), 0, JPEG, {"centre_mm": 1500}))
        runtime.depth_camera = worker  # type: ignore[assignment]
        runtime.control = Socket()  # type: ignore[assignment]
        await runtime.command({**message, "id": "b"})
        assert worker.active and runtime.depth_enabled
        runtime.control = Socket((json.dumps({"kind": "heartbeat", "depth": True}),))  # type: ignore[assignment]
        await runtime._control_reader()
        assert worker.renewed == 1
        socket = Socket()
        task = asyncio.create_task(runtime._capture_sender(socket, "depth", None))  # type: ignore[arg-type]
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        packet = socket.sent[0]
        assert isinstance(packet, bytes)
        size = struct.unpack_from("!I", packet)[0]
        meta = json.loads(packet[4 : 4 + size])
        assert meta["sequence"] == 5 and meta["centre_mm"] == 1500 and packet[4 + size :] == JPEG
        runtime.control = Socket()  # type: ignore[assignment]
        await runtime.command(
            {**message, "id": "c", "args": {"channel": "depth", "enabled": False}}
        )
        assert not worker.active and not runtime.depth_enabled

    asyncio.run(scenario())


def test_depth_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = AgentRuntime({"depth_openni": str(tmp_path), "pose_python": "/py"})
    runtime.board.status = lambda: {"wheels_ready": False, "gimbal_ready": False}  # type: ignore[method-assign]
    monkeypatch.setattr("tvpet_pi.agent.Path.read_text", lambda self: "")
    monkeypatch.setattr("tvpet_pi.agent.system_status", lambda: {})
    assert runtime.status()["depth_camera"]["state"] == "unknown"
    (tmp_path / "libOpenNI2.so").touch()
    assert runtime.status()["depth_camera"]["state"] == "available"
    assert AgentRuntime({}).status()["depth_camera"]["state"] == "disabled"
