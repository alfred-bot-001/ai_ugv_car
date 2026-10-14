"""Exact source frames, bounded inference, failure recovery and session invalidation."""

import asyncio
import json
import struct
import time

import httpx
import pytest
from tvpet_console.faces import FaceVideo, Frame, Recognition, validate_service_url


def unpack(packet: bytes) -> tuple[dict, bytes]:
    size = struct.unpack_from("!I", packet)[0]
    return json.loads(packet[4 : 4 + size]), packet[4 + size :]


async def output(video: FaceVideo, version: int) -> tuple[dict, bytes]:
    async with video.output_event:
        await asyncio.wait_for(
            video.output_event.wait_for(lambda: video.version > version), timeout=2
        )
    return unpack(video.packet)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://127.0.0.1:8000/path",
        "http://user:password@localhost",
        "file://localhost",
        "http://localhost/?redirect=external",
        "http://localhost/#fragment",
    ],
)
def test_only_local_configured_service(url: str) -> None:
    with pytest.raises(ValueError):
        validate_service_url(url)
    assert validate_service_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert validate_service_url("") == ""


def test_family_labels_unknown_and_untrusted_attributes() -> None:
    faces = [
        {"name": name, "confidence": score, "bbox": [10, 20, 80, 100], "age": 99}
        for name, score in [
            ("max", 0.8),
            ("son", 0.7),
            ("wife", 0.6),
            ("unknown", 0.7),
            ("max", 0.3),
        ]
    ]
    result = Recognition.model_validate({"faces": faces, "time_ms": 64})
    assert [box["label"] for box in result.boxes()] == ["爸爸", "儿子", "妈妈", "未登记", "未登记"]
    assert result.boxes()[0]["known"] and not result.boxes()[-1]["known"]
    assert "age" not in result.boxes()[0]
    assert Recognition.model_validate({"faces": [], "time_ms": 10}).boxes() == []


@pytest.mark.parametrize("bbox", [[1, 2, 3], [3, 2, 1, 4], [1, 1, float("nan"), 3]])
def test_invalid_service_coordinates(bbox: list) -> None:
    with pytest.raises(ValueError):
        Recognition.model_validate(
            {"faces": [{"name": "max", "confidence": 0.8, "bbox": bbox}], "time_ms": 10}
        )


def test_worker_matches_source_and_drops_queued_frames() -> None:
    async def scenario() -> None:
        started, resume = asyncio.Event(), asyncio.Event()
        calls = []

        async def recognize(request: httpx.Request) -> httpx.Response:
            calls.append(request.content)
            if len(calls) == 1:
                started.set()
                await resume.wait()
            return httpx.Response(
                200,
                json={
                    "faces": [{"name": "son", "confidence": 0.8, "bbox": [10, 20, 40, 80]}],
                    "time_ms": 70,
                },
            )

        video = FaceVideo("http://127.0.0.1:8000", transport=httpx.MockTransport(recognize))
        video.reset(True)
        task = asyncio.create_task(video.run())
        try:
            video.submit(b"first-source-jpeg", 1)
            await asyncio.wait_for(started.wait(), 1)
            video.submit(b"discard-this-jpeg", 2)
            video.submit(b"latest-source-jpeg", 3)
            resume.set()
            meta, jpeg = await output(video, 0)
            assert meta["sequence"] == 1 and jpeg == b"first-source-jpeg"
            assert meta["faces"][0]["label"] == "儿子"
            meta, jpeg = await output(video, video.version)
            assert meta["sequence"] == 3 and jpeg == b"latest-source-jpeg"
            assert len(calls) == 2 and b"discard-this-jpeg" not in calls[1]
            assert video.status() == {"state": "active", "inference_ms": 70}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "invalid", "http"])
def test_service_failure_video_fallback_and_recovery(failure: str) -> None:
    async def scenario() -> None:
        calls = []

        async def recognize(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                if failure == "timeout":
                    raise httpx.ReadTimeout("timeout")
                return httpx.Response(503 if failure == "http" else 200, json={"invalid": True})
            return httpx.Response(200, json={"faces": [], "time_ms": 42})

        video = FaceVideo("http://localhost:8000", transport=httpx.MockTransport(recognize))
        video.reset(True)
        task = asyncio.create_task(video.run())
        try:
            video.submit(b"fallback-jpeg", 1)
            meta, jpeg = await output(video, 0)
            assert meta["recognition"] == "unavailable" and meta["faces"] == []
            assert jpeg == b"fallback-jpeg"
            version = video.version
            video.submit(b"live-during-backoff", 2)
            meta, jpeg = await output(video, version)
            assert jpeg == b"live-during-backoff" and len(calls) == 1
            video.retry_at = 0
            version = video.version
            video.submit(b"recovered-jpeg", 3)
            meta, jpeg = await output(video, version)
            assert meta["recognition"] == "active" and jpeg == b"recovered-jpeg"
            assert len(calls) == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_stopped_session_rejects_late_result_and_stale_frame() -> None:
    async def scenario() -> None:
        started, resume = asyncio.Event(), asyncio.Event()

        async def recognize(request: httpx.Request) -> httpx.Response:
            started.set()
            await resume.wait()
            return httpx.Response(200, json={"faces": [], "time_ms": 80})

        video = FaceVideo("http://localhost:8000", transport=httpx.MockTransport(recognize))
        video.submit(b"unauthorized", 0)
        assert video.latest is None
        video.reset(True)
        task = asyncio.create_task(video.run())
        try:
            video.submit(b"old-session", 1)
            await started.wait()
            video.reset()
            video.reset(True)
            resume.set()
            await asyncio.sleep(0.25)
            assert video.packet == b"" and video.version == 0 and video.state == "waiting"
            old = Frame(b"stale", 2, time.monotonic() - 2, video.generation)
            await video.publish(old, "active", [], 0)
            assert video.packet == b""
            video.submit(b"new-session", 3)
            meta, jpeg = await output(video, 0)
            assert meta["sequence"] == 3 and jpeg == b"new-session"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_disabled_recognition_still_streams_video() -> None:
    async def scenario() -> None:
        video = FaceVideo()
        video.reset(True)
        task = asyncio.create_task(video.run())
        try:
            video.submit(b"raw-video", 1)
            meta, jpeg = await output(video, 0)
            assert meta["recognition"] == "disabled" and jpeg == b"raw-video"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
