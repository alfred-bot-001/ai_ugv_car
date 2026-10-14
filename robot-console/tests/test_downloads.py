"""Disconnects and cancelled archive builders do not leave private ZIP files."""

import asyncio
import threading
from pathlib import Path

import pytest
from starlette.types import Message
from tvpet_console.downloads import ExportDownloads, TemporaryFileResponse


@pytest.mark.parametrize("send_failure", [False, True])
def test_response_cleanup_after_transfer_or_disconnect(tmp_path: Path, send_failure: bool) -> None:
    path = tmp_path / "private.zip"
    path.write_bytes(b"private-export")

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and send_failure:
            raise OSError("client disconnected")

    async def exercise() -> None:
        response = TemporaryFileResponse(path)
        if send_failure:
            with pytest.raises(OSError, match="client disconnected"):
                await response({"type": "http", "method": "GET", "headers": []}, receive, send)
        else:
            await response({"type": "http", "method": "GET", "headers": []}, receive, send)
        assert not path.exists()

    asyncio.run(exercise())


def test_cancelled_builder_finishes_and_removes_export(tmp_path: Path) -> None:
    path = tmp_path / "private.zip"
    started = threading.Event()
    release = threading.Event()

    def builder() -> Path:
        started.set()
        assert release.wait(timeout=5)
        path.write_bytes(b"private-export")
        return path

    async def exercise() -> None:
        downloads = ExportDownloads()
        request = asyncio.create_task(downloads.build(builder))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        finally:
            release.set()
        await downloads.close()
        assert not path.exists()

    asyncio.run(exercise())
