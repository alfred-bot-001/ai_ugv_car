"""Daily sampling quota governs background capture separately from manual video."""

import asyncio
import time
from unittest.mock import AsyncMock

from tvpet_console.gateway import Gateway


def test_quota_stop_manual_preview_and_new_day_resume() -> None:
    async def exercise() -> None:
        gateway = Gateway(background_video=True)
        gateway.control = AsyncMock()
        gateway.state_at = time.monotonic()
        gateway.rpc = AsyncMock(return_value={"ok": True})
        policy = AsyncMock(return_value=True)
        gateway.background_policy = policy
        await gateway.refresh_background_capture()
        assert gateway.background_video and gateway.faces.active
        policy.return_value = False
        gateway.video = b"old-frame"
        await gateway.refresh_background_capture()
        assert not gateway.background_video and not gateway.faces.active
        assert not gateway.video
        gateway.rpc.assert_called_with("media.set", {"channel": "video", "enabled": False})
        await gateway.viewer("video", 1)
        await gateway.refresh_background_capture()
        assert not gateway.background_video and gateway.faces.active
        gateway.rpc.assert_called_with("media.set", {"channel": "video", "enabled": True})
        await gateway.viewer("video", -1)
        assert not gateway.faces.active
        gateway.rpc.assert_called_with("media.set", {"channel": "video", "enabled": False})
        policy.return_value = True
        await gateway.refresh_background_capture()
        assert gateway.background_video and gateway.faces.active
        gateway.rpc.assert_called_with("media.set", {"channel": "video", "enabled": True})

    asyncio.run(exercise())


def test_quota_storage_failure_stops_automatic_capture() -> None:
    async def exercise() -> None:
        gateway = Gateway(background_video=True)
        gateway.control = AsyncMock()
        gateway.state_at = time.monotonic()
        gateway.rpc = AsyncMock(return_value={"ok": True})
        gateway.background_policy = AsyncMock(side_effect=OSError("quota storage failed"))
        gateway.faces.reset(True)
        await gateway.refresh_background_capture()
        assert not gateway.background_video and not gateway.faces.active
        gateway.rpc.assert_called_with("media.set", {"channel": "video", "enabled": False})

    asyncio.run(exercise())
