"""Front Orbbec depth: worker framing, the child-process adapter and the depth channel."""

import asyncio
import io
import json
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tvpet_pi import protocol
from tvpet_pi.agent import AgentRuntime
from tvpet_pi.depth import DepthCamera, DepthFrame
from tvpet_pi.depth_wire import HEADER, MAX_JPEG, encode_frame, read_frame

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


# What pose-venv offers the worker: none of the agent's dependencies, fake camera modules.
POSE_VENV_IMPORT = """
import sys, types
sys.modules["pydantic"] = None
openni = types.ModuleType("openni")
openni._openni2 = types.ModuleType("openni._openni2")
openni.openni2 = types.ModuleType("openni.openni2")
sys.modules.update({"cv2": types.ModuleType("cv2"), "numpy": types.ModuleType("numpy"),
                    "openni": openni, "openni._openni2": openni._openni2,
                    "openni.openni2": openni.openni2})
import tvpet_pi.depth_worker
"""


DRIVER_NOISE = """
import os
from tvpet_pi.depth_wire import encode_frame
out = tvpet_pi.depth_worker.frame_output()
print("Warning: USB events thread", flush=True)
os.write(1, b"ReadBinaryFile, 63\\n")
out.write(encode_frame({"sequence": 1}, b"\\xff\\xd8x"))
out.flush()
"""


def test_worker_imports_without_the_agents_dependencies() -> None:
    # 0.12.0 shipped a worker that pulled pydantic in through tvpet_pi.depth and died on start.
    run = subprocess.run([sys.executable, "-c", POSE_VENV_IMPORT], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert MAX_JPEG == protocol.MAX_JPEG


def test_driver_output_stays_off_the_frame_stream() -> None:
    # Orbbec's driver prints to stdout; 0.12.0 mixed those lines into the frames.
    run = subprocess.run(
        [sys.executable, "-c", POSE_VENV_IMPORT + DRIVER_NOISE], capture_output=True
    )
    assert run.returncode == 0, run.stderr
    assert read_frame(io.BytesIO(run.stdout)) == ({"sequence": 1}, b"\xff\xd8x")
    assert b"USB events" in run.stderr and b"ReadBinaryFile" in run.stderr


FAKE_WORKER = """
import sys, time
from tvpet_pi.depth_wire import encode_frame
for n in (1, 2):
    sys.stdout.buffer.write(encode_frame({"sequence": n, "centre_mm": 800 + n}, b"\\xff\\xd8x"))
    sys.stdout.buffer.flush()
sys.stdin.buffer.read()  # like the real worker: exit when the agent closes stdin
"""


def test_camera_reads_the_worker_and_stops_it() -> None:
    camera = DepthCamera("python", "/openni", command=[sys.executable, "-c", FAKE_WORKER])
    assert DepthCamera("/py", "/o").command[:3] == ["/py", "-m", "tvpet_pi.depth_worker"]
    assert "--floor" not in DepthCamera("/py", "/o").command
    assert DepthCamera("/py", "/o", "/f.json").command[-2:] == ["--floor", "/f.json"]
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
        self.started = 0
        self.started_at = 0.0

    def start(self) -> None:
        self.active = True
        self.started += 1
        self.started_at = time.monotonic()

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


def test_navigation_keeps_depth_on_and_gets_fresh_scans_only(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = AgentRuntime({"depth_openni": str(tmp_path), "pose_python": "/py"})
        scan = {"angle_min": -0.6, "angle_increment": 0.0175, "range_max": 2.0, "ranges_mm": [0]}
        fresh = DepthFrame(7, time.monotonic_ns(), 0, JPEG, {"centre_mm": 900, "scan": scan})
        worker = Worker(fresh)
        runtime.depth_camera = worker  # type: ignore[assignment]
        now = time.monotonic()
        await runtime.depth_tick(now)
        assert not worker.started  # nobody asked yet
        reply = runtime.nav_depth_scan()  # the bridge's board status poll
        assert reply == {"sequence": 7, "age": reply["age"], **scan} and reply["age"] < 0.5
        await runtime.depth_tick(time.monotonic())
        assert worker.active and worker.started == 1
        await runtime.depth_tick(time.monotonic())
        assert worker.renewed == 1 and worker.started == 1
        # The depth view closing does not stop what Nav2 still uses.
        runtime.control = Socket()  # type: ignore[assignment]
        message = {"id": "a", "action": "media.set", "args": {"channel": "depth", "enabled": False}}
        await runtime.command(message)
        assert worker.active
        # Stale or uncalibrated frames give no scan.
        worker.value = DepthFrame(8, time.monotonic_ns() - 2 * 10**9, 0, JPEG, {"scan": scan})
        assert runtime.nav_depth_scan() is None
        worker.value = DepthFrame(9, time.monotonic_ns(), 0, JPEG, {"centre_mm": 900})
        assert runtime.nav_depth_scan() is None
        # Once the bridge stops asking, depth goes off.
        await runtime.depth_tick(runtime.depth_nav_until + 0.1)
        assert not worker.active
        # A worker that failed is not restarted in a tight loop.
        runtime.depth_nav_until = time.monotonic() + 30
        worker.error, worker.started_at = "device busy", time.monotonic()
        await runtime.depth_tick(time.monotonic())
        assert worker.started == 1
        await runtime.depth_tick(time.monotonic() + 11)
        assert worker.started == 2

    asyncio.run(scenario())
