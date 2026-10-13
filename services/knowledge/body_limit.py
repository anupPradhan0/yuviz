"""Request-body size cap for the retrieval endpoints.

FastAPI parses (and, on a 422, echoes) the whole body before any field limit applies, so an authenticated caller
could make every request cost tens of milliseconds of event-loop time with a multi-megabyte query. This pure ASGI
middleware answers 413 for the listed paths from the Content-Length header, and again while streaming if the
header is absent or lies, without ever echoing the body."""

from __future__ import annotations

import os
from typing import Any

DEFAULT_LIMIT_BYTES = int(os.environ.get("KNOWLEDGE_MAX_BODY_BYTES", str(64 * 1024)))
LIMITED_PATHS = {"/internal/retrieve": DEFAULT_LIMIT_BYTES, "/internal/prewarm": DEFAULT_LIMIT_BYTES}


class _TooLarge(BaseException):
    """BaseException on purpose: FastAPI's body reader turns any Exception into a 400 \"error parsing the body\"."""


class BodyLimitMiddleware:
    def __init__(self, app: Any, limits: dict[str, int] | None = None) -> None:
        self.app = app
        self.limits = LIMITED_PATHS if limits is None else limits

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        limit = self.limits.get(scope.get("path", "")) if scope["type"] == "http" else None
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(send)
            return
        received = 0
        started = False

        async def limited_receive() -> dict:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge
            return message

        async def tracking_send(message: dict) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if not started:
                await self._reject(send)

    @staticmethod
    async def _reject(send: Any) -> None:
        body = b'{"detail":"request body too large"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
