"""Outbound authenticated WSS transport; capture and safety have separate owners."""

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import re
import secrets
import ssl
import struct
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any

from websockets.client import WebSocketClientProtocol, connect
from websockets.exceptions import WebSocketException
from websockets.typing import Subprotocol

from .capture import Camera, CaptureWorker, Frame, Microphone
from .hardware import Board, system_status
from .m2 import M2Link
from .nav import NavBridge
from .pose_process import PoseProcess
from .protocol import AGENT_PROTOCOL, Stick
from .safety import Guard

LOGGER = logging.getLogger(__name__)
M2_MICROPHONE = "hw:CARD=XFMDPV0018,DEV=0"


def alsa_card_present(device: str, cards: str) -> bool:
    """True when the CARD=<id> named by an ALSA device string is in /proc/asound/cards."""
    match = re.search(r"CARD=([^,]+)", device)
    ids = re.findall(r"^\s*\d+\s+\[(\S+)\s*\]", cards, re.MULTILINE)
    return match is not None and match.group(1) in ids


class AgentRuntime:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.pose = PoseProcess(
            str(config.get("pose_python", "")), str(config.get("pose_model", ""))
        )
        self.guard = Guard()
        evidence = Path(str(config["motion_evidence"])) if config.get("motion_evidence") else None
        self.board = Board(str(config.get("uart", "/dev/ttyAMA0")), self.guard, evidence)
        nav_dir = config.get("nav_dir")
        self.nav = NavBridge(self.board, Path(str(nav_dir))) if nav_dir else None
        # A plain USB microphone has no M2 serial link; set "m2" to "" for it.
        m2_path = str(config.get("m2", "/dev/m2_mic"))
        self.m2 = M2Link(m2_path) if m2_path else None
        self.camera = CaptureWorker(Camera(str(config.get("camera", "/dev/video0"))))
        self.microphone_device = str(config.get("microphone", M2_MICROPHONE))
        self.microphone = CaptureWorker(Microphone(self.microphone_device))
        self.speaker_device = str(config.get("speaker", "plughw:CARD=Device,DEV=0"))
        self.snapshot_id = ""
        self.snapshot_min_version = 0
        self.snapshot_until = 0.0
        self.snapshot_requested_ns = 0
        self.video_enabled = False
        self.audio_enabled = False
        self.playback: subprocess.Popen[bytes] | None = None
        self.audio_dbfs: float | None = None
        self.control: WebSocketClientProtocol | None = None
        self.send_lock = asyncio.Lock()
        self.playback_epoch = secrets.token_hex(16)
        self.playback_lock = threading.RLock()

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            if self.control:
                await asyncio.wait_for(self.control.send(json.dumps(message)), timeout=0.5)

    def status(self) -> dict[str, Any]:
        cards = Path("/proc/asound/cards").read_text()
        board = self.board.status()
        return {
            "mode": "control" if board["wheels_ready"] and board["gimbal_ready"] else "observe",
            "playback_epoch": self.playback_epoch,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "system": system_status(),
            "board": board,
            "camera": {
                "state": "fault"
                if self.camera.error
                else "active"
                if self.camera.active
                else "available"
                if Path(str(self.config.get("camera", "/dev/video0"))).exists()
                else "unknown",
                "error": self.camera.error,
                "format": "MJPEG 640×480 / 10fps",
            },
            "microphone": {
                "state": "fault"
                if self.microphone.error
                else "active"
                if self.microphone.active
                else "available"
                if alsa_card_present(self.microphone_device, cards)
                else "unknown",
                "format": ("M2" if self.m2 else "USB 麦克风") + " · 16kHz / 单声道 / S16_LE",
                "dbfs": self.audio_dbfs,
                "signal": "quiet"
                if self.audio_dbfs is not None and self.audio_dbfs < -65
                else "signal",
                "error": self.microphone.error,
            },
            "speaker": {
                "state": "active"
                if self.playback and self.playback.poll() is None
                else "available"
                if alsa_card_present(self.speaker_device, cards)
                else "unknown"
            },
            "edge": {
                "state": self.pose.state,
                "model": "MediaPipe Pose Lite",
                "inference_ms": self.pose.inference_ms,
            },
        }

    def stop_playback(self) -> None:
        with self.playback_lock:
            self._stop_playback()

    def _stop_playback(self) -> None:
        self.playback_epoch = secrets.token_hex(16)
        if self.playback:
            with contextlib.suppress(ProcessLookupError):
                self.playback.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.playback.wait(timeout=0.5)
            if self.playback.poll() is None:
                self.playback.kill()
            self.playback = None

    def play(self, payload: bytes, args: dict[str, Any]) -> None:
        with self.playback_lock:
            self._play(payload, args)

    def _play(self, payload: bytes, args: dict[str, Any]) -> None:
        if (
            args.get("epoch") != self.playback_epoch
            or args.get("length") != len(payload)
            or self.guard.challenges.pop(str(args.get("challenge")), 0) <= time.monotonic()
        ):
            raise ValueError("playback request expired or revoked")
        if len(payload) > 2_000_000 or hashlib.sha256(payload).hexdigest() != args.get("sha256"):
            raise ValueError("invalid playback payload")
        with wave.open(io.BytesIO(payload)) as wav:
            if (
                wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or wav.getframerate() != 16000
                or wav.getnframes() > 16000 * 60
            ):
                raise ValueError("unsupported playback WAV")
        self._stop_playback()
        self.playback = subprocess.Popen(
            ["aplay", "-q", "-D", self.speaker_device],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert self.playback.stdin is not None
        threading.Thread(
            target=self._feed_playback, args=(self.playback, payload), daemon=True
        ).start()

    @staticmethod
    def _feed_playback(process: subprocess.Popen[bytes], payload: bytes) -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(payload)
        except (OSError, BrokenPipeError):
            pass
        finally:
            with contextlib.suppress(OSError):
                process.stdin.close()

    async def command(self, message: dict[str, Any]) -> None:
        action, args = message.get("action"), message.get("args", {})
        result: dict[str, Any] = {"kind": "result", "id": message.get("id"), "ok": True}
        try:
            if action in ("stop", "estop"):
                self.board.stop(emergency=action == "estop")
                self.stop_playback()
            elif action == "reset":
                self.board.stop()
                self.guard.reset()
            elif action == "lease":
                result["lease"] = await asyncio.to_thread(self.board.acquire)
            elif action == "stick":
                self.board.move(Stick.model_validate(args))
            elif action == "media.set":
                channel, enabled = args.get("channel"), args.get("enabled") is True
                if channel not in ("video", "audio"):
                    raise ValueError("unknown media channel")
                worker = self.camera if channel == "video" else self.microphone
                if channel == "video":
                    self.video_enabled = enabled
                else:
                    self.audio_enabled = enabled
                if enabled and not worker.active:
                    if channel == "audio" and self.m2:
                        self.m2.wake()
                    await asyncio.to_thread(worker.start)
                elif not enabled:
                    await asyncio.to_thread(worker.stop)
            elif action == "snapshot":
                self.snapshot_id = str(args["snapshot_id"])
                self.snapshot_min_version = self.camera.version
                self.snapshot_until = time.monotonic() + 2
                self.snapshot_requested_ns = time.monotonic_ns()
                if not self.camera.active:
                    await asyncio.to_thread(self.camera.start)
            else:
                raise ValueError("unsupported action")
        except (ValueError, OSError, KeyError, wave.Error) as error:
            result.update(ok=False, error=str(error))
        await self.send(result)

    async def _control_reader(self) -> None:
        assert self.control is not None
        async for raw in self.control:
            message = json.loads(raw)
            if message.get("kind") == "heartbeat":
                self.guard.heartbeat()
                if message.get("video") is True and self.video_enabled:
                    self.camera.renew()
                if message.get("audio") is True and self.audio_enabled:
                    self.microphone.renew()
            elif message.get("kind") == "command":
                await self.command(message)

    async def _status_sender(self) -> None:
        last_status = 0.0
        while True:
            await self.send(
                {"kind": "challenge", "value": self.guard.challenge(), "lease": self.guard.lease}
            )
            if time.monotonic() - last_status >= 1:
                await self.send({"kind": "status", "state": await asyncio.to_thread(self.status)})
                last_status = time.monotonic()
            await asyncio.sleep(0.15)

    async def _media_sender(self, channel: str, link: str, tls: ssl.SSLContext, token: str) -> None:
        base_url = str(self.config["url"]).rstrip("/")
        agent_id = str(self.config.get("agent_id", "laosan"))
        async with connect(
            f"{base_url}/internal/v1/agents/{agent_id}/{channel}",
            ssl=tls,
            extra_headers={"Authorization": f"Bearer {token}", "X-Agent-Link": link},
            subprotocols=[Subprotocol(AGENT_PROTOCOL)],
            max_size=2_010_000,
            ping_interval=10,
            ping_timeout=5,
            close_timeout=1,
        ) as socket:
            receiver = (
                asyncio.create_task(self._playback_receiver(socket)) if channel == "audio" else None
            )
            try:
                await self._capture_sender(socket, channel, receiver)
            finally:
                if receiver:
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)

    async def _playback_receiver(self, socket: WebSocketClientProtocol) -> None:
        async for packet in socket:
            if not isinstance(packet, bytes) or len(packet) < 4:
                raise ValueError("invalid playback packet")
            length = struct.unpack_from("!I", packet)[0]
            if not 0 < length <= 4096:
                raise ValueError("invalid playback metadata")
            meta = json.loads(packet[4 : 4 + length])
            result = {"kind": "result", "id": meta.get("id"), "ok": True}
            try:
                await asyncio.to_thread(self.play, packet[4 + length :], meta)
            except (OSError, ValueError, wave.Error) as error:
                result.update(ok=False, error=str(error))
            await self.send(result)

    async def _capture_sender(
        self, socket: WebSocketClientProtocol, channel: str, receiver: asyncio.Task[None] | None
    ) -> None:
        worker = self.camera if channel == "video" else self.microphone
        last = -1
        while True:
            if receiver and receiver.done():
                receiver.result()
                return
            version, value = worker.latest()
            if value is not None and version != last:
                last = version
                if isinstance(value, Frame):
                    started = time.monotonic()
                    pose = await self.pose.infer(value.jpeg)
                    if time.monotonic_ns() - value.timestamp_ns > 1_500_000_000:
                        continue
                    snapshot_id = ""
                    if (
                        self.snapshot_id
                        and version > self.snapshot_min_version
                        and value.timestamp_ns >= self.snapshot_requested_ns
                    ):
                        snapshot_id, self.snapshot_id = self.snapshot_id, ""
                    metadata = json.dumps(
                        {
                            "sequence": value.sequence,
                            "timestamp_ns": value.timestamp_ns,
                            "flags": value.flags,
                            "snapshot_id": snapshot_id,
                            "pose": pose,
                        }
                    ).encode()
                    await asyncio.wait_for(
                        socket.send(struct.pack("!I", len(metadata)) + metadata + value.jpeg),
                        timeout=0.3,
                    )
                    await asyncio.sleep(max(0, 0.2 - (time.monotonic() - started)))
                else:
                    import math

                    samples = struct.unpack("<320h", value)
                    rms = math.sqrt(sum(s * s for s in samples) / 320)
                    self.audio_dbfs = 20 * math.log10(max(rms, 1) / 32768)
                    await asyncio.wait_for(socket.send(value), timeout=0.05)
            if channel == "video" and not self.video_enabled and not self.snapshot_id:
                if worker.active and time.monotonic() >= self.snapshot_until:
                    await asyncio.to_thread(worker.stop)
            await asyncio.sleep(0.005)

    async def run(self) -> None:
        tls = ssl.create_default_context(cafile=str(self.config["ca_file"]))
        token_path = Path(str(self.config["token_file"]))
        if token_path.stat().st_mode & 0o077:
            raise ValueError("agent token file must have mode 0600")
        token = token_path.read_text().strip()
        if not str(self.config["url"]).startswith("wss://"):
            raise ValueError("WSS is required")
        self.board.open()
        if self.nav and self.board.serial is not None:
            self.nav.start()
        if self.m2:
            self.m2.open()
        try:
            while True:
                tasks: list[asyncio.Task[None]] = []
                try:
                    agent_id = str(self.config.get("agent_id", "laosan"))
                    url = str(self.config["url"]).rstrip("/")
                    async with connect(
                        f"{url}/internal/v1/agents/{agent_id}/control",
                        ssl=tls,
                        extra_headers={"Authorization": f"Bearer {token}"},
                        subprotocols=[Subprotocol(AGENT_PROTOCOL)],
                        max_size=65536,
                        ping_interval=10,
                        ping_timeout=5,
                        close_timeout=1,
                    ) as socket:
                        self.control = socket
                        welcome = json.loads(await socket.recv())
                        link = str(welcome["link_id"])
                        tasks = [
                            asyncio.create_task(self._control_reader()),
                            asyncio.create_task(self._status_sender()),
                            asyncio.create_task(self._media_sender("video", link, tls, token)),
                            asyncio.create_task(self._media_sender("audio", link, tls, token)),
                        ]
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                except (OSError, ValueError, TimeoutError, KeyError, WebSocketException) as error:
                    LOGGER.warning("Device connection failed: %s", type(error).__name__)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    self.control = None
                    await self.pose.stop()
                    self.guard.disconnect()
                    with contextlib.suppress(OSError, ValueError):
                        self.board.stop()
                    self.stop_playback()
                    await asyncio.to_thread(self.camera.stop)
                    await asyncio.to_thread(self.microphone.stop)
                    self.video_enabled = self.audio_enabled = False
                    self.snapshot_id = ""
                await asyncio.sleep(2)
        finally:
            self.board.close()
            self.stop_playback()
            if self.m2:
                self.m2.close()


def run_agent(config_path: Path) -> None:
    config = json.loads(config_path.read_text())
    logging.basicConfig(level=logging.INFO)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(AgentRuntime(config).run())
