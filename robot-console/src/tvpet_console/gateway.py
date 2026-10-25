"""Independent device channels; all state and media are transient and bounded."""

import asyncio
import contextlib
import hashlib
import json
import secrets
import struct
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from tvpet_pi.navframe import NavCache
from tvpet_pi.protocol import MAX_AUDIO, MAX_JPEG, PoseResult

from .faces import FaceVideo


class DeviceError(ValueError):
    """The car refused a command; `code` lets the browser react without a toast."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class Gateway:
    def __init__(self, face_service_url: str = "", background_video: bool = False) -> None:
        self.background_video = background_video
        self.background_configured = background_video
        self.background_policy: Callable[[], Awaitable[bool]] | None = None
        self.faces = FaceVideo(face_service_url)
        self.control: WebSocket | None = None
        self.link_id = ""
        self.state: dict[str, Any] = {}
        self.state_at = 0.0
        self.challenge = ""
        self.challenge_lease = ""
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.send_lock = asyncio.Lock()
        self.video_event = asyncio.Condition()
        self.audio_event = asyncio.Condition()
        self.video: bytes = b""
        self.video_version = 0
        self.video_metadata: dict[str, Any] = {}
        self.audio = b""
        self.audio_version = 0
        # Forward Orbbec color camera: frames only (no face or pose processing).
        self.front_event = asyncio.Condition()
        self.front = b""
        self.front_version = 0
        # Front Orbbec depth: the whole device packet (distances + colorized JPEG).
        self.depth_event = asyncio.Condition()
        self.depth = b""
        self.depth_version = 0
        self.snapshots: dict[str, asyncio.Future[bytes]] = {}
        self.viewer_counts = {"video": 0, "audio": 0, "nav": 0, "front": 0, "depth": 0}
        # Map page feed: memory only (the map is the home's floor plan), cleared offline.
        self.nav = NavCache()
        self.nav_event = asyncio.Condition()
        self.viewer_lock = asyncio.Lock()
        self.channels: dict[str, WebSocket] = {}

    @property
    def online(self) -> bool:
        return self.control is not None and time.monotonic() - self.state_at < 5

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            if self.control is None:
                raise ValueError("老三离线")
            try:
                await asyncio.wait_for(self.control.send_json(message), timeout=0.5)
            except (WebSocketDisconnect, RuntimeError, OSError) as error:
                raise ValueError("老三连接已断开") from error

    async def rpc(
        self, action: str, args: dict[str, Any] | None = None, timeout: float = 2
    ) -> dict[str, Any]:
        if not self.online or len(self.pending) >= 16:
            raise ValueError("老三离线或设备忙")
        request_id = secrets.token_hex(16)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send(
                {"kind": "command", "id": request_id, "action": action, "args": args or {}}
            )
            result = await asyncio.wait_for(future, timeout)
            if not result.get("ok"):
                raise DeviceError(
                    str(result.get("error", "设备拒绝操作")), str(result.get("code", ""))
                )
            return result
        finally:
            self.pending.pop(request_id, None)

    async def control_loop(self, websocket: WebSocket) -> None:
        self.control = websocket
        self.link_id = secrets.token_urlsafe(24)
        heartbeat: asyncio.Task[None] | None = None
        try:
            await self.send({"kind": "welcome", "link_id": self.link_id})
            heartbeat = asyncio.create_task(self._heartbeat())
            while True:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
                if not isinstance(message, dict):
                    raise ValueError("invalid agent message")
                kind = message.get("kind")
                if kind == "status" and isinstance(message.get("state"), dict):
                    self.state = message["state"]
                    self.state_at = time.monotonic()
                elif kind == "challenge":
                    self.challenge = str(message.get("value", ""))[:128]
                    self.challenge_lease = str(message.get("lease", ""))[:128]
                elif kind == "result":
                    future = self.pending.get(str(message.get("id")))
                    if future is not None and not future.done():
                        future.set_result(message)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat
            self.control = None
            self.link_id = ""
            self.challenge = ""
            self.challenge_lease = ""
            self.video = b""
            self.audio = b""
            self.front = b""
            self.depth = b""
            self.nav.clear()
            self.faces.reset()
            for pending_result in self.pending.values():
                if not pending_result.done():
                    pending_result.set_exception(ValueError("老三连接已断开"))
            for pending_frame in self.snapshots.values():
                if not pending_frame.done():
                    pending_frame.set_exception(ValueError("老三连接已断开"))
            for channel in list(self.channels.values()):
                with contextlib.suppress(Exception):
                    await channel.close(code=1012)
            self.channels.clear()

    async def _heartbeat(self) -> None:
        while True:
            await self.send(
                {
                    "kind": "heartbeat",
                    "video": (self.viewer_counts["video"] > 0 or self.background_video),
                    "audio": self.viewer_counts["audio"] > 0,
                    "nav": self.viewer_counts["nav"] > 0,
                    "front": self.viewer_counts["front"] > 0,
                    "depth": self.viewer_counts["depth"] > 0,
                }
            )
            await asyncio.sleep(0.2)

    async def media_loop(self, websocket: WebSocket, channel: str) -> None:
        if channel in self.channels:
            raise ValueError("duplicate media channel")
        self.channels[channel] = websocket
        try:
            while True:
                packet = await websocket.receive_bytes()
                if channel == "audio":
                    if len(packet) != MAX_AUDIO:
                        raise ValueError("invalid PCM packet")
                    async with self.audio_event:
                        self.audio = packet
                        self.audio_version += 1
                        self.audio_event.notify_all()
                else:
                    if not 4 < len(packet) <= MAX_JPEG + 32768:
                        raise ValueError("invalid video packet")
                    size = struct.unpack_from("!I", packet)[0]
                    if not 1 <= size <= 32768:
                        raise ValueError("invalid video metadata")
                    meta = json.loads(packet[4 : 4 + size])
                    if not isinstance(meta, dict) or not isinstance(meta.get("sequence"), int):
                        raise ValueError("invalid frame metadata")
                    jpeg = packet[4 + size :]
                    if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > MAX_JPEG:
                        raise ValueError("invalid JPEG")
                    if channel == "depth":
                        async with self.depth_event:
                            self.depth = packet
                            self.depth_version += 1
                            self.depth_event.notify_all()
                        continue
                    if channel == "front":
                        async with self.front_event:
                            self.front = jpeg
                            self.front_version += 1
                            self.front_event.notify_all()
                        continue
                    self.video_metadata = meta
                    snapshot_id = str(meta.get("snapshot_id", ""))
                    future = self.snapshots.get(snapshot_id)
                    if future and not future.done():
                        future.set_result(jpeg)
                    async with self.video_event:
                        self.video = jpeg
                        self.video_version += 1
                        self.video_event.notify_all()
                    pose = PoseResult.model_validate(
                        meta.get("pose", {"state": "disabled"})
                    ).model_dump()
                    self.faces.submit(
                        jpeg, meta["sequence"], pose, int(meta.get("timestamp_ns", 0))
                    )
        finally:
            if self.channels.get(channel) is websocket:
                self.channels.pop(channel, None)
                if channel == "video":
                    self.faces.reset(self.viewer_counts["video"] > 0 or self.background_video)

    async def nav_loop(self, websocket: WebSocket) -> None:
        """Map page packets from pi-agent: validated, cached, then announced to viewers."""
        if "nav" in self.channels:
            raise ValueError("duplicate nav channel")
        self.channels["nav"] = websocket
        try:
            while True:
                self.nav.add(await websocket.receive_bytes())
                async with self.nav_event:
                    self.nav_event.notify_all()
        finally:
            if self.channels.get("nav") is websocket:
                self.channels.pop("nav", None)
                self.nav.clear()

    async def viewer(self, channel: str, delta: int) -> None:
        async with self.viewer_lock:
            await self._viewer(channel, delta)

    async def _viewer(self, channel: str, delta: int) -> None:
        next_count = max(0, self.viewer_counts[channel] + delta)
        if delta > 0 and not self.online:
            raise ValueError("老三离线")
        if channel == "nav":  # the heartbeat carries it; pi-agent needs no command
            self.viewer_counts[channel] = next_count
            return
        if self.online:
            await self.rpc(
                "media.set",
                {
                    "channel": channel,
                    "enabled": next_count > 0 or (channel == "video" and self.background_video),
                },
            )
        self.viewer_counts[channel] = next_count
        if channel == "video" and (next_count == 0 or (delta > 0 and next_count == 1)):
            self.faces.reset(next_count > 0 or self.background_video)
        if delta > 0 and next_count == 1:
            if channel == "video":
                self.video = b""
            elif channel == "front":
                self.front = b""
            elif channel == "depth":
                self.depth = b""
            else:
                self.audio = b""

    async def snapshot(self) -> bytes:
        if len(self.snapshots) >= 2:
            raise ValueError("拍照正在进行")
        request_id = secrets.token_hex(16)
        future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self.snapshots[request_id] = future
        try:
            await self.rpc("snapshot", {"snapshot_id": request_id})
            return await asyncio.wait_for(future, timeout=2)
        finally:
            self.snapshots.pop(request_id, None)

    async def play_audio(self, wav: bytes) -> None:
        """Playback uses the audio channel; acknowledgments stay on control."""
        socket = self.channels.get("audio")
        if not self.online or socket is None or len(wav) > 2_000_000:
            raise ValueError("老三音频通道不可用")
        request_id = secrets.token_hex(16)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        meta = json.dumps(
            {
                "id": request_id,
                "sha256": hashlib.sha256(wav).hexdigest(),
                "epoch": self.state.get("playback_epoch", ""),
                "challenge": self.challenge,
                "length": len(wav),
            }
        ).encode()
        try:
            await asyncio.wait_for(
                socket.send_bytes(struct.pack("!I", len(meta)) + meta + wav), 0.3
            )
            result = await asyncio.wait_for(future, 2)
            if not result.get("ok"):
                raise ValueError(str(result.get("error", "播放被拒绝")))
        finally:
            self.pending.pop(request_id, None)

    async def revoke(self) -> None:
        if self.online:
            with contextlib.suppress(ValueError, TimeoutError):
                await self.rpc("stop")
            for channel in ("video", "audio", "front", "depth"):
                with contextlib.suppress(ValueError, TimeoutError):
                    await self.rpc(
                        "media.set",
                        {
                            "channel": channel,
                            "enabled": channel == "video" and self.background_video,
                        },
                    )
        self.video = b""
        self.audio = b""
        self.front = b""
        self.depth = b""
        self.faces.reset(self.background_video)

    async def background_capture(self) -> None:
        while True:
            await self.refresh_background_capture()
            await asyncio.sleep(1)

    async def refresh_background_capture(self) -> None:
        """Daily quota stops the automatic lease; explicit viewers keep their own lease."""
        allowed = self.background_configured
        if allowed and self.background_policy is not None:
            try:
                allowed = await self.background_policy()
            except (OSError, ValueError):
                allowed = False
        async with self.viewer_lock:
            self.background_video = allowed
            enabled = allowed or self.viewer_counts["video"] > 0
            if not enabled and self.faces.active:
                self.faces.reset(False)
                self.video = b""
                self.video_metadata = {}
            if self.background_configured and self.online:
                try:
                    await self.rpc("media.set", {"channel": "video", "enabled": enabled})
                    if enabled and not self.faces.active:
                        self.faces.reset(True)
                except (ValueError, TimeoutError):
                    pass

    def status(self) -> dict[str, Any]:
        age = time.monotonic() - self.state_at if self.state_at else None
        return {
            "online": self.online,
            "age_seconds": age,
            "state": self.state if self.online else {},
            "challenge": self.challenge if self.online else "",
            "channels": list(self.channels) if self.online else [],
        }
