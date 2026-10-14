"""Reject oversized HTTP bodies before accumulating them in memory."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyTooLarge(Exception):
    pass


class LimitBody:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith("/api/"):
            await self.app(scope, receive, send)
            return
        count = 0

        async def bounded_receive() -> Message:
            nonlocal count
            message = await receive()
            count += len(message.get("body", b""))
            if count > 16384:
                raise BodyTooLarge
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except BodyTooLarge:
            await JSONResponse({"detail": "请求过大"}, status_code=413)(scope, receive, send)
