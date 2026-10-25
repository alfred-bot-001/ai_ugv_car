"""Bounded V4L2 MJPEG and ALSA PCM capture, with explicit acquisition lifetime."""

import contextlib
import fcntl
import mmap
import os
import select
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .protocol import MAX_JPEG


def ioctl_code(number: int, size: int, direction: int = 3) -> int:
    return (direction << 30) | (size << 16) | (ord("V") << 8) | number


@dataclass(frozen=True)
class Frame:
    sequence: int
    timestamp_ns: int
    flags: int
    jpeg: bytes


MJPEG_PADDING = 64


def mjpeg_payload(data: bytes) -> bytes:
    """A whole JPEG from one driver buffer, without the zero padding some cameras add.

    The front Orbbec (Sonix bridge) pads each frame to 8 bytes after the EOI marker
    (seen 2026-10-21: 3-5 zero bytes). Anything else after EOI is still rejected.
    """
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("invalid MJPEG frame")
    end = data.rfind(b"\xff\xd9", max(0, len(data) - MJPEG_PADDING - 2))
    if end < 0 or data[end + 2 :].strip(b"\x00"):
        raise ValueError("invalid MJPEG frame")
    return data[: end + 2]


class Camera:
    """Linux 64-bit single-plane MMAP ABI, matching videodev2.h on the target Pi."""

    def __init__(self, path: str = "/dev/video0") -> None:
        self.path = path
        self.fd = -1
        self.maps: list[mmap.mmap] = []

    def open(self) -> None:
        if struct.calcsize("P") != 8:
            raise RuntimeError("V4L2 adapter requires 64-bit Linux")
        self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fmt = bytearray(208)
            struct.pack_into("I", fmt, 0, 1)
            struct.pack_into("IIII", fmt, 8, 640, 480, int.from_bytes(b"MJPG", "little"), 0)
            fcntl.ioctl(self.fd, ioctl_code(5, 208), fmt)
            if struct.unpack_from("III", fmt, 8) != (640, 480, int.from_bytes(b"MJPG", "little")):
                raise RuntimeError("camera rejected MJPEG 640x480")
            rate = bytearray(204)
            struct.pack_into("I", rate, 0, 1)
            struct.pack_into("II", rate, 12, 1, 10)
            fcntl.ioctl(self.fd, ioctl_code(22, 204), rate)
            buffers = bytearray(20)
            struct.pack_into("III", buffers, 0, 2, 1, 1)
            fcntl.ioctl(self.fd, ioctl_code(8, 20), buffers)
            count = struct.unpack_from("I", buffers)[0]
            if not 1 <= count <= 8:
                raise RuntimeError("invalid V4L2 buffer count")
            for index in range(count):
                buffer = self._buffer(index)
                fcntl.ioctl(self.fd, ioctl_code(9, 88), buffer)
                length = struct.unpack_from("I", buffer, 72)[0]
                offset = struct.unpack_from("I", buffer, 64)[0]
                self.maps.append(mmap.mmap(self.fd, length, offset=offset))
                fcntl.ioctl(self.fd, ioctl_code(15, 88), buffer)
            fcntl.ioctl(self.fd, ioctl_code(18, 4, 1), struct.pack("I", 1))
        except Exception:
            self.close()
            raise

    @staticmethod
    def _buffer(index: int = 0) -> bytearray:
        buffer = bytearray(88)
        struct.pack_into("II", buffer, 0, index, 1)
        struct.pack_into("I", buffer, 60, 1)
        return buffer

    def read(self) -> Frame:
        if not select.select([self.fd], [], [], 1)[0]:
            raise TimeoutError("camera frame timeout")
        buffer = self._buffer()
        fcntl.ioctl(self.fd, ioctl_code(17, 88), buffer)
        index, _, size, flags = struct.unpack_from("IIII", buffer)
        try:
            if index >= len(self.maps) or not 1 <= size <= MAX_JPEG:
                raise ValueError("invalid camera buffer")
            seconds, micros = struct.unpack_from("qq", buffer, 24)
            sequence = struct.unpack_from("I", buffer, 56)[0]
            jpeg = mjpeg_payload(self.maps[index][:size])
            if flags & 0x2000 != 0x2000:
                raise ValueError("driver did not provide monotonic timestamp")
            timestamp = seconds * 1_000_000_000 + micros * 1000
            if not 0 <= time.monotonic_ns() - timestamp <= 2_000_000_000:
                raise ValueError("stale camera driver frame")
            return Frame(sequence, timestamp, flags, jpeg)
        finally:
            fcntl.ioctl(self.fd, ioctl_code(15, 88), buffer)

    def close(self) -> None:
        if self.fd >= 0:
            with contextlib.suppress(OSError):
                fcntl.ioctl(self.fd, ioctl_code(19, 4, 1), struct.pack("I", 1))
            for region in self.maps:
                region.close()
            self.maps.clear()
            os.close(self.fd)
            self.fd = -1


class Microphone:
    """One channel at 16kHz S16_LE; use a plughw device for microphones that only run at 48kHz."""

    def __init__(self, device: str = "hw:CARD=XFMDPV0018,DEV=0") -> None:
        self.device = device
        self.process: subprocess.Popen[bytes] | None = None

    def open(self) -> None:
        self.process = subprocess.Popen(
            [
                "arecord",
                "-q",
                "-D",
                self.device,
                "-t",
                "raw",
                "-f",
                "S16_LE",
                "-r",
                "16000",
                "-c",
                "1",
                "--period-size=320",
                "--buffer-size=1280",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def read(self) -> bytes:
        if not self.process or not self.process.stdout:
            raise ValueError("microphone is closed")
        if not select.select([self.process.stdout], [], [], 1)[0]:
            raise TimeoutError("microphone capture timeout")
        chunk = self.process.stdout.read(640)
        if len(chunk) != 640:
            raise ValueError("microphone audio stream ended")
        return bytes(chunk)

    def close(self) -> None:
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
            if self.process.stdout:
                self.process.stdout.close()
            self.process = None


if TYPE_CHECKING:
    from .depth import DepthCamera


class CaptureWorker:
    """Thread owns its adapter; only newest sample is held, never an unbounded queue."""

    def __init__(self, adapter: "Camera | Microphone | DepthCamera") -> None:
        self.adapter = adapter
        self.value: Frame | bytes | None = None
        self.version = 0
        self.error = ""
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.started_at = 0.0
        self.deadline = 0.0

    def start(self) -> None:
        self.stop()
        self.value = None
        self.error = ""
        self.stop_event.clear()
        self.started_at = time.monotonic()
        self.deadline = self.started_at + 2
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def renew(self) -> None:
        self.deadline = time.monotonic() + 2

    def _run(self) -> None:
        try:
            self.adapter.open()
            while not self.stop_event.is_set() and time.monotonic() < self.deadline:
                value = self.adapter.read()
                with self.lock:
                    self.value = value
                    self.version += 1
        except (OSError, ValueError, TimeoutError, RuntimeError) as error:
            self.error = str(error)
        finally:
            self.adapter.close()
            with self.lock:
                self.value = None

    def latest(self) -> tuple[int, Frame | bytes | None]:
        with self.lock:
            return self.version, self.value

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.5)
        self.thread = None

    @property
    def active(self) -> bool:
        return self.thread is not None and self.thread.is_alive()
