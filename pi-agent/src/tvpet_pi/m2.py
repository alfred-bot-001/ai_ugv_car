"""M2 handshake and wake protocol from the supplied R818 manufacturer manual."""

import contextlib
import json
import struct
import threading
import time
from typing import Any

import serial


def encode(kind: int, message_id: int, payload: bytes) -> bytes:
    packet = b"\xa5\x01" + bytes([kind]) + struct.pack("<HH", len(payload), message_id) + payload
    return packet + bytes([-sum(packet) & 255])


class Decoder:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, chunk: bytes) -> list[tuple[int, int, bytes]]:
        self.buffer.extend(chunk)
        packets: list[tuple[int, int, bytes]] = []
        while len(self.buffer) >= 8:
            start = self.buffer.find(b"\xa5\x01")
            if start < 0:
                self.buffer = self.buffer[-1:]
                break
            del self.buffer[:start]
            if len(self.buffer) < 8:
                break
            length, message_id = struct.unpack_from("<HH", self.buffer, 3)
            if length > 16384:
                del self.buffer[:2]
                continue
            if len(self.buffer) < length + 8:
                break
            packet = bytes(self.buffer[: length + 8])
            del self.buffer[: length + 8]
            if sum(packet) & 255 == 0:
                packets.append((packet[2], message_id, packet[7:-1]))
        return packets


class M2Link:
    """ACK within the receive thread; no gain/firmware/wake-word settings are changed."""

    def __init__(self, path: str = "/dev/m2_mic") -> None:
        self.path = path
        self.serial: serial.Serial | None = None
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.last_seen = 0.0
        self.last_event: dict[str, Any] = {}
        self.error = ""
        self.message_id = 0

    def open(self) -> None:
        try:
            self.serial = serial.Serial(
                self.path, 115200, timeout=0.02, write_timeout=0.05, exclusive=True
            )
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        except (OSError, serial.SerialException) as error:
            self.error = str(error)

    def _run(self) -> None:
        decoder = Decoder()
        assert self.serial is not None
        while not self.closed.is_set():
            try:
                for kind, message_id, payload in decoder.feed(self.serial.read(2048)):
                    self.last_seen = time.monotonic()
                    if kind != 0xFF:
                        with self.lock:
                            self.serial.write(encode(0xFF, message_id, b"\xa5\x00\x00\x00"))
                    if kind == 4:
                        with contextlib.suppress(ValueError):
                            self.last_event = json.loads(payload)
            except (OSError, serial.SerialException) as error:
                self.error = str(error)
                return

    def wake(self) -> None:
        if self.serial is None:
            raise ValueError("M2 串口不可用，无法执行授权唤醒")
        # Only transient manual wake; retains factory array type and gain settings.
        payload = json.dumps({"type": "manual_wakeup", "content": {"beam": 0}}).encode()
        with self.lock:
            self.message_id = (self.message_id + 1) % 65536
            self.serial.write(encode(5, self.message_id, payload))

    def close(self) -> None:
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=0.2)
        if self.serial:
            self.serial.close()
