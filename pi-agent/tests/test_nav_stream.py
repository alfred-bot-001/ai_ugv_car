"""Map page feed on the Pi: packet checks, the cache, the relay socket and forwarding."""

import asyncio
import contextlib
import json
import socket
import struct
import tempfile
import threading
import time
from pathlib import Path

import pytest
from tvpet_pi.agent import AgentRuntime
from tvpet_pi.nav_stream import NavStream
from tvpet_pi.navframe import PNG_SIGNATURE, NavCache, make_packet, parse_packet


def raw(meta: dict, payload: bytes = b"") -> bytes:
    body = json.dumps(meta).encode()
    return struct.pack("!I", len(body)) + body + payload


def test_packets_are_checked() -> None:
    assert parse_packet(raw({"kind": "pose", "x": 1}))[0] == "pose"
    assert parse_packet(raw({"kind": "map"}, PNG_SIGNATURE + b"data"))[0] == "map"
    for bad in (
        b"\x00\x00",
        raw({"kind": "teleport"}),
        raw({"kind": "pose"}, b"extra"),
        raw({"kind": "map"}, b"not a png"),
        raw({"kind": "map"}, PNG_SIGNATURE + b"x" * 1_048_577),
        struct.pack("!I", 99) + b"{}",
        raw(["kind"]),  # type: ignore[arg-type]
        struct.pack("!I", 22) + b'{"kind":"pose","x":NaN}',
    ):
        with pytest.raises(ValueError):
            parse_packet(bad)
    assert json.loads(make_packet("log", {"text": "hi", "kind": "spoofed"})[4:])["kind"] == "log"


def test_cache_snapshot_and_backlog() -> None:
    cache = NavCache(logs=3, backlog=4)
    assert cache.snapshot() == (0, []) and cache.since(0) == (0, [])
    cache.add(raw({"kind": "pose", "x": 0}))
    cache.add(raw({"kind": "map"}, PNG_SIGNATURE))
    for i in range(4):
        cache.add(raw({"kind": "log", "text": str(i)}))
    seq, packets = cache.snapshot()
    kinds = [json.loads(p[4:])["kind"] for p in packets if not p.endswith(PNG_SIGNATURE)]
    assert seq == 6 and packets[0].endswith(PNG_SIGNATURE)  # map first, then pose, then logs
    assert kinds == ["pose", "log", "log", "log"]
    assert cache.since(5) == (6, [packets[-1]])
    assert cache.since(1) == (6, None)  # fell behind the 4-packet backlog
    assert cache.since(99) == (6, None)
    cache.clear()
    assert cache.snapshot() == (6, [])


def test_relay_socket_keeps_good_packets_and_drops_bad_framing() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        closed = threading.Event()
        stream = NavStream(Path(short), closed)
        thread = stream.start()
        path = Path(short) / "stream.sock"
        for _ in range(50):
            if path.exists():
                break
            time.sleep(0.01)
        assert path.stat().st_mode & 0o777 == 0o600
        with socket.socket(socket.AF_UNIX) as relay:
            relay.connect(str(path))
            for data in (raw({"kind": "pose", "x": 1}), raw({"kind": "bogus"})):
                relay.sendall(struct.pack("!I", len(data)) + data)
            relay.sendall(struct.pack("!I", 3))  # impossible length: pi-agent hangs up
            for _ in range(100):
                if stream.received and stream.rejected:
                    break
                time.sleep(0.01)
            assert relay.recv(10) == b""
        assert (stream.received, stream.rejected) == (1, 1)
        stream.log("WARN", "安全停车：stall_body")
        assert parse_packet(stream.cache.logs[-1])[1]["node"] == "pi-agent"
        closed.set()
        thread.join(timeout=2)
        assert not path.exists()


def test_safety_stop_reaches_the_map_log(tmp_path: Path) -> None:
    runtime = AgentRuntime({"nav_dir": str(tmp_path / "nav")})
    assert runtime.nav is not None and runtime.nav_stream is not None
    runtime.nav.halt("stall_body")
    meta = parse_packet(runtime.nav_stream.cache.logs[-1])[1]
    assert meta["level"] == "WARN" and meta["text"] == "安全停车：stall_body"
    agent = parse_packet(runtime.nav_agent_packet())[1]
    assert agent["fault"]["reason"] == "stall_body" and agent["relay"] is False
    runtime.nav.notify = lambda reason: 1 / 0  # a broken listener never breaks the stop
    runtime.nav.halt("localization_lost")
    assert runtime.nav.fault["reason"] == "localization_lost"


class Channel:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.sent.append(data)


class Control:
    """The Mini's control link, delivering the given messages."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = iter(messages)

    def __aiter__(self) -> "Control":
        return self

    async def __anext__(self) -> str:
        try:
            return next(self.messages)
        except StopIteration:
            raise StopAsyncIteration from None


def test_forwarding_only_while_the_page_is_open(tmp_path: Path, monkeypatch) -> None:
    runtime = AgentRuntime({"nav_dir": str(tmp_path / "nav"), "url": "wss://mini.test"})
    assert runtime.nav_stream is not None
    cache = runtime.nav_stream.cache
    cache.add(raw({"kind": "map"}, PNG_SIGNATURE))
    channel = Channel()

    @contextlib.asynccontextmanager
    async def fake_connect(url, **kwargs):
        assert url == "wss://mini.test/internal/v1/agents/laosan/nav"
        assert kwargs["extra_headers"]["X-Agent-Link"] == "link"
        yield channel

    monkeypatch.setattr("tvpet_pi.agent.connect", fake_connect)

    async def scenario() -> None:
        task = asyncio.create_task(runtime._nav_sender("link", None, "token"))
        await asyncio.sleep(0.3)
        assert channel.sent == []  # nobody watching
        runtime.control = Control([json.dumps({"kind": "heartbeat", "nav": True})])
        await runtime._control_reader()
        await asyncio.sleep(0.15)
        kinds = [parse_packet(p)[0] for p in channel.sent]
        assert kinds[:2] == ["map", "agent"]
        cache.add(raw({"kind": "pose", "x": 2}))
        await asyncio.sleep(0.15)
        assert parse_packet(channel.sent[-1])[0] == "pose"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_map_channel_failures_stay_inside_it(tmp_path: Path, monkeypatch) -> None:
    runtime = AgentRuntime({"nav_dir": str(tmp_path / "nav"), "url": "wss://mini.test"})

    def refuse(*args, **kwargs):
        raise OSError("refused")

    monkeypatch.setattr("tvpet_pi.agent.connect", refuse)

    async def scenario() -> None:
        task = asyncio.create_task(runtime._nav_channel("link", None, "token"))
        await asyncio.sleep(0.05)
        assert not task.done()  # retrying quietly, the control link keeps running
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
