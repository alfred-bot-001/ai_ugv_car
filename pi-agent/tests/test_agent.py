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
from tvpet_pi.agent import AgentRuntime, alsa_card_present
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


def test_usb_microphone_without_m2(monkeypatch) -> None:
    cards = (
        " 2 [ugvspk         ]: USB-Audio - USB PnP Audio Device\n"
        " 3 [ugvmic         ]: USB-Audio - USB Composite Device\n"
    )
    assert alsa_card_present("plughw:CARD=ugvmic,DEV=0", cards)
    assert not alsa_card_present("hw:CARD=XFMDPV0018,DEV=0", cards)
    assert not alsa_card_present("default", cards)

    async def scenario():
        runtime = AgentRuntime(
            {
                "m2": "",
                "microphone": "plughw:CARD=ugvmic,DEV=0",
                "speaker": "plughw:CARD=ugvspk,DEV=0",
            }
        )
        assert runtime.m2 is None
        assert runtime.microphone.adapter.device == "plughw:CARD=ugvmic,DEV=0"
        runtime.microphone = Worker()
        runtime.control = Socket()
        await runtime.command(
            {"id": "a", "action": "media.set", "args": {"channel": "audio", "enabled": True}}
        )
        assert json.loads(runtime.control.sent[-1])["ok"] and runtime.microphone.active

    asyncio.run(scenario())


def test_lights_command_switches_the_board_and_reports_the_state() -> None:
    async def scenario():
        runtime = AgentRuntime({})
        runtime.board = Mock()
        runtime.board.set_lights.return_value = {"base": True, "head": False}
        runtime.control = Socket()
        await runtime.command({"id": "l1", "action": "lights", "args": {"base": True}})
        await runtime.command({"id": "l2", "action": "lights", "args": {"base": "bright"}})
        results = [json.loads(raw) for raw in runtime.control.sent]
        assert results[0]["ok"] and results[0]["lights"] == {"base": True, "head": False}
        assert runtime.board.set_lights.call_args.args[0].base is True
        assert not results[1]["ok"]
        assert runtime.board.set_lights.call_count == 1

    asyncio.run(scenario())


def sleepy_runtime(lidar: str = "on", container: str = "exited", mode: str = "odometry"):
    from tvpet_pi.sleep import IDLE_SECONDS

    runtime = AgentRuntime({})
    runtime.board = Mock()
    runtime.board.set_lights.return_value = {"base": False, "head": False}
    runtime.control = Socket()
    runtime.navd = Mock()
    runtime.navd.request.return_value = {"ok": True, "lidar": lidar}
    runtime.navd_status = {
        "online": True, "container": container, "mode": mode, "map": None, "job": None,
        "lidar": lidar,
    }  # fmt: skip
    return runtime, IDLE_SECONDS


def test_a_command_wakes_a_sleeping_car() -> None:
    async def scenario():
        runtime, idle = sleepy_runtime(lidar="off")
        assert await runtime.sleep_tick(0.0) is None
        assert await runtime.sleep_tick(idle) == "sleep"
        assert runtime.sleeper.asleep
        runtime.navd.request.reset_mock()
        await runtime.command({"id": "l1", "action": "lights", "args": {"base": True}})
        assert runtime.navd.request.call_args.args[0] == {"op": "wake"}
        assert not runtime.sleeper.asleep
        await runtime.command({"id": "l2", "action": "lights", "args": {"head": True}})
        assert runtime.navd.request.call_count == 1  # awake already: no second wake

    asyncio.run(scenario())


def test_a_quiet_half_hour_cuts_lidar_power_and_the_lights() -> None:
    async def scenario():
        runtime, idle = sleepy_runtime()
        await runtime.sleep_tick(0.0)
        runtime.video_enabled = True
        assert await runtime.sleep_tick(idle) is None  # someone is watching
        runtime.video_enabled = False
        assert await runtime.sleep_tick(2 * idle - 1) is None
        assert runtime.power_status(2 * idle - 1) == {
            "state": "awake", "idle_seconds": idle - 1, "lidar": "on"
        }  # fmt: skip
        assert await runtime.sleep_tick(2 * idle) == "sleep"
        assert runtime.navd.request.call_args.args[0] == {"op": "standby"}
        lights = runtime.board.set_lights.call_args.args[0]
        assert lights.base is False and lights.head is False
        assert runtime.power_status(2 * idle)["state"] == "asleep"

    asyncio.run(scenario())


def test_navigation_work_keeps_the_car_awake() -> None:
    async def scenario():
        runtime, idle = sleepy_runtime(container="running healthy", mode="mapping")
        await runtime.sleep_tick(0.0)
        assert await runtime.sleep_tick(idle) is None  # mapping: the map is not saved
        runtime.navd_status.update(mode="localization", job={"state": "running", "op": "goal"})
        assert await runtime.sleep_tick(2 * idle) is None  # a goal is running
        runtime.navd_status["job"]["state"] = "ok"
        assert await runtime.sleep_tick(3 * idle - 1) is None
        assert await runtime.sleep_tick(3 * idle) == "sleep"  # localization is dropped
        assert runtime.navd.request.call_args.args[0] == {"op": "standby"}

    asyncio.run(scenario())


def test_lidar_power_follows_the_sleep_state_after_a_navd_restart() -> None:
    async def scenario():
        runtime, idle = sleepy_runtime(lidar="unknown")
        await runtime.sleep_tick(0.0)  # awake and navd does not know: power it on
        assert runtime.navd.request.call_args.args[0] == {"op": "wake"}
        runtime.navd_status["lidar"] = "on"
        runtime.navd.request.reset_mock()
        await runtime.sleep_tick(10.0)
        assert runtime.navd.request.call_count == 0
        assert await runtime.sleep_tick(idle) == "sleep"
        runtime.navd_status["lidar"] = "unknown"  # ugv-navd restarted while asleep
        runtime.navd.request.reset_mock()
        runtime.navd.request.side_effect = ValueError("「休眠」还没完成，请稍候")
        await runtime.sleep_tick(idle + 10)  # asks again; a refusal is only logged
        assert runtime.navd.request.call_args.args[0] == {"op": "standby"}

    asyncio.run(scenario())
