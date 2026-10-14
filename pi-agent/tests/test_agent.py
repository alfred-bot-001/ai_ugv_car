"""Command allowlist, playback replay rejection and isolated capture transport."""

import asyncio
import hashlib
import io
import json
import struct
import time
import wave
from unittest.mock import Mock

import pytest
from tvpet_pi.agent import AgentRuntime
from tvpet_pi.capture import Frame


class Worker:
    def __init__(self, value=None):
        self.active = False
        self.error = ""
        self.version = 1
        self.value = value

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def renew(self):
        pass

    def latest(self):
        return self.version, self.value


class Socket:
    def __init__(self, packets=()):
        self.sent = []
        self.packets = iter(packets)

    async def send(self, message):
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.packets)
        except StopIteration:
            raise StopAsyncIteration from None


def test_commands_capture_stop_and_unknown(monkeypatch) -> None:
    async def scenario():
        runtime = AgentRuntime({})
        runtime.board = Mock()
        runtime.board.acquire.return_value = "test-lease"
        runtime.m2 = Mock()
        runtime.camera = Worker()
        runtime.microphone = Worker()
        runtime.control = Socket()
        for action, args in [
            ("lease", {}),
            ("media.set", {"channel": "audio", "enabled": True}),
            ("media.set", {"channel": "video", "enabled": True}),
            ("snapshot", {"snapshot_id": "test-id"}),
            ("stop", {}),
            ("estop", {}),
            ("reset", {}),
            ("media.set", {"channel": "audio", "enabled": False}),
            ("media.set", {"channel": "video", "enabled": False}),
            ("media.set", {"channel": "bad", "enabled": True}),
            ("raw.uart", {}),
        ]:
            await runtime.command({"id": action, "action": action, "args": args})
        results = [json.loads(raw) for raw in runtime.control.sent]
        assert results[0]["lease"] == "test-lease"
        assert not results[-1]["ok"] and not results[-2]["ok"]
        runtime.m2.wake.assert_called_once()
        assert not runtime.camera.active and not runtime.microphone.active
        runtime.control = Socket(
            [
                json.dumps({"kind": "heartbeat", "audio": True, "video": True}),
                json.dumps({"kind": "command", "id": "unknown", "action": "unknown"}),
            ]
        )
        await runtime._control_reader()
        runtime.control = Socket()
        task = asyncio.create_task(runtime._status_sender())
        monkeypatch.setattr(runtime, "status", lambda: {"mode": "observe"})
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert any(json.loads(raw).get("kind") == "challenge" for raw in runtime.control.sent)

    asyncio.run(scenario())


def test_playback_hash_epoch_and_wav(monkeypatch) -> None:
    runtime = AgentRuntime({})
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(3200))
    payload = output.getvalue()
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr("tvpet_pi.agent.subprocess.Popen", Mock(return_value=process))
    meta = {
        "epoch": runtime.playback_epoch,
        "challenge": runtime.guard.challenge(),
        "length": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    runtime.play(payload, meta)
    process.stdin.write.assert_called_once_with(payload)
    with pytest.raises(ValueError):
        runtime.play(payload, meta)
    meta.update(epoch=runtime.playback_epoch, challenge=runtime.guard.challenge(), sha256="bad")
    with pytest.raises(ValueError, match="invalid playback"):
        runtime.play(payload, meta)
    runtime.stop_playback()
    process.terminate.assert_called_once()
    process.kill.assert_called_once()


def test_video_snapshot_new_frame_and_audio_latest() -> None:
    async def scenario():
        runtime = AgentRuntime({})
        runtime.video_enabled = True
        runtime.snapshot_id = "request"
        runtime.snapshot_requested_ns = time.monotonic_ns()
        runtime.snapshot_min_version = 0
        runtime.camera = Worker(
            Frame(1, runtime.snapshot_requested_ns + 1, 0x2000, b"\xff\xd8jpeg\xff\xd9")
        )
        socket = Socket()
        task = asyncio.create_task(runtime._capture_sender(socket, "video", None))
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        packet = socket.sent[0]
        length = struct.unpack_from("!I", packet)[0]
        assert json.loads(packet[4 : 4 + length])["snapshot_id"] == "request"
        assert runtime.snapshot_id == ""
        runtime.microphone = Worker(bytes(640))
        socket = Socket()
        task = asyncio.create_task(runtime._capture_sender(socket, "audio", None))
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert socket.sent == [bytes(640)] and runtime.audio_dbfs < -65

    asyncio.run(scenario())
