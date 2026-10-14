"""Private temporary exports are removed even if the browser disconnects."""

import asyncio
from collections.abc import Callable
from pathlib import Path

from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send


class TemporaryFileResponse(FileResponse):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)


class ExportDownloads:
    def __init__(self) -> None:
        self._pending: set[asyncio.Task[Path]] = set()

    @staticmethod
    def _discard(task: asyncio.Task[Path]) -> None:
        if not task.cancelled() and task.exception() is None:
            task.result().unlink(missing_ok=True)

    async def build(self, builder: Callable[[], Path]) -> Path:
        task = asyncio.create_task(asyncio.to_thread(builder))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A running worker thread cannot be cancelled: keep it alive until cleanup.
            task.add_done_callback(self._discard)
            raise

    async def close(self) -> None:
        pending = tuple(self._pending)
        for task in pending:
            task.add_done_callback(self._discard)
        await asyncio.gather(*pending, return_exceptions=True)
