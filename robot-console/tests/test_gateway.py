"""Transport isolation, bounded data, cancellation and disconnect cleanup."""

import asyncio
import hashlib
import json
import struct
from typing import Any

import pytest
from tvpet_console.gateway import Gateway


class Socket:
    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        self.messages: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[Any] = []
        self.closed = False

    async def receive_json(self) -> Any:
        return await self.messages.get()

    async def receive_bytes(self) -> bytes:
        result = await self.messages.get()
        if isinstance(result, Exception):
            raise result
        return result

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if message.get("kind") == "command":
            await self.messages.put({"kind": "result", "id": message["id"], "ok": True})
            if message["action"] == "snapshot":
                self.gateway.snapshots[message["args"]["snapshot_id"]].set_result(b"fresh jpeg")

    async def send_bytes(self, payload: bytes) -> None:
        self.sent.append(payload)
        length = struct.unpack_from("!I", payload)[0]
        meta = json.loads(payload[4 : 4 + length])
        assert hashlib.sha256(payload[4 + length :]).hexdigest() == meta["sha256"]
        self.gateway.pending[meta["id"]].set_result({"ok": True})

    async def close(self, **kwargs: Any) -> None:
        self.closed = True


def test_device_rpc_snapshot_audio_and_disconnect() -> None:
    async def scenario() -> None:
        gateway = Gateway()
        with pytest.raises(ValueError):
            await gateway.rpc("stop")
        control = Socket(gateway)
        task = asyncio.create_task(gateway.control_loop(control))
        await control.messages.put({"kind": "status", "state": {"system": {"temperature_c": 47}}})
        await control.messages.put({"kind": "challenge", "value": "test-challenge"})
        await asyncio.sleep(0.01)
        assert gateway.online and gateway.challenge == "test-challenge"
        assert gateway.status()["state"]["system"]["temperature_c"] == 47
        await asyncio.gather(gateway.viewer("video", 1), gateway.viewer("video", 1))
        assert gateway.viewer_counts["video"] == 2 and gateway.faces.active
        await gateway.viewer("video", -1)
        assert gateway.viewer_counts["video"] == 1 and gateway.faces.active
        assert await gateway.snapshot() == b"fresh jpeg"
        audio = Socket(gateway)
        gateway.channels["audio"] = audio
        await gateway.play_audio(b"bounded wav")
        assert isinstance(audio.sent[0], bytes)
        await gateway.revoke()
        await gateway.viewer("video", -1)
        assert not gateway.faces.active and not gateway.faces.packet
        assert not gateway.video and not gateway.audio
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not gateway.online and not gateway.link_id and audio.closed
        assert gateway.pending == {} and gateway.snapshots == {}
        with pytest.raises(ValueError):
            await gateway.viewer("video", 1)
        assert gateway.viewer_counts["video"] == 0

    asyncio.run(scenario())


def test_media_validation_and_bounded_latest_samples() -> None:
    async def scenario() -> None:
        gateway = Gateway()
        audio = Socket(gateway)
        await audio.messages.put(bytes(640))
        await audio.messages.put(bytes([1]) * 640)
        await audio.messages.put(b"bad size")
        with pytest.raises(ValueError):
            await gateway.media_loop(audio, "audio")
        assert len(gateway.audio) == 640 and gateway.audio[0] == 1
        assert "audio" not in gateway.channels
        meta = json.dumps({"sequence": 3, "snapshot_id": ""}).encode()
        video = Socket(gateway)
        await video.messages.put(struct.pack("!I", len(meta)) + meta + b"\xff\xd8jpeg\xff\xd9")
        await video.messages.put(struct.pack("!I", 9000) + b"x" * 10)
        with pytest.raises(ValueError):
            await gateway.media_loop(video, "video")
        assert gateway.video_version == 1 and gateway.video_metadata["sequence"] == 3
        assert "video" not in gateway.channels

    asyncio.run(scenario())


def test_failed_heartbeat_and_welcome_release_control_slot() -> None:
    async def scenario() -> None:
        for failure in ("welcome", "heartbeat"):
            gateway = Gateway()

            class FailingSocket(Socket):
                fail_on = failure

                async def send_json(self, message: dict[str, Any]) -> None:
                    if message["kind"] == self.fail_on:
                        raise OSError("connection lost")
                    await super().send_json(message)

            socket = FailingSocket(gateway)
            task = asyncio.create_task(gateway.control_loop(socket))
            await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert gateway.control is None and not gateway.link_id
            replacement = Socket(gateway)
            new_task = asyncio.create_task(gateway.control_loop(replacement))
            await replacement.messages.put({"kind": "status", "state": {}})
            await asyncio.sleep(0.01)
            assert gateway.online
            new_task.cancel()
            await asyncio.gather(new_task, return_exceptions=True)

    asyncio.run(scenario())
