"""Bounded IPC with an isolated low-priority inference process."""

import asyncio
import json
import os
import struct
import time
from typing import Any


class PoseProcess:
    def __init__(self, python: str = "", model: str = "") -> None:
        self.python, self.model = python, model
        self.process: asyncio.subprocess.Process | None = None
        self.retry_at = 0.0
        self.state = "idle" if python and model else "disabled"
        self.inference_ms: float | None = None

    async def stop(self) -> None:
        process, self.process = self.process, None
        if process:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def infer(self, jpeg: bytes) -> dict[str, Any]:
        if not self.python or not self.model:
            return {"state": "disabled", "people": []}
        if time.monotonic() < self.retry_at:
            return {"state": "unavailable", "people": []}
        try:
            if self.process is None:
                self.process = await asyncio.create_subprocess_exec(
                    self.python,
                    "-m",
                    "tvpet_pi.pose_worker",
                    self.model,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=None,
                    limit=32768,
                    env={
                        **os.environ,
                        "OMP_NUM_THREADS": "2",
                        "OPENBLAS_NUM_THREADS": "1",
                        "TF_NUM_INTRAOP_THREADS": "2",
                        "TF_NUM_INTEROP_THREADS": "1",
                    },
                )
                assert self.process.stdout
                ready = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 20))
                if ready.get("ready") is not True:
                    raise ValueError("pose initialization failed")
            assert self.process.stdin and self.process.stdout
            self.process.stdin.write(struct.pack("!I", len(jpeg)) + jpeg)
            await asyncio.wait_for(self.process.stdin.drain(), 1)
            result: dict[str, Any] = json.loads(
                await asyncio.wait_for(self.process.stdout.readline(), 2)
            )
            if result.get("state") != "active" or not isinstance(result.get("people"), list):
                raise ValueError("invalid pose response")
            self.state, self.inference_ms = "active", result.get("inference_ms")
            return result
        except (OSError, ValueError, TimeoutError, AssertionError):
            await self.stop()
            self.state, self.inference_ms = "unavailable", None
            self.retry_at = time.monotonic() + 10
            return {"state": "unavailable", "people": []}
