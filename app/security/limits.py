"""Request-body size limits that cannot be bypassed.

A ``Content-Length`` check alone is not enough: a client can stream a body with ``Transfer-Encoding: chunked``
(no length header) and FastAPI would read all of it - into RAM for JSON, into temp files for multipart - before
any route code runs. This pure-ASGI middleware rejects oversized declared lengths up front AND counts the bytes
as they arrive, stopping the upload with 413 the moment it passes the limit.
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
JSON_LIMIT = 2 * 1024 * 1024  # chat messages, answers, settings - far below this


def _too_large(limit: int) -> dict:
    size = f"{limit / (1024 * 1024):.0f} MB" if limit >= 1024 * 1024 else f"{limit // 1024} KB"
    return {"detail": {"code": "TOO_LARGE", "message": f"Request too large (max {size})."}}


class BodySizeLimit:
    def __init__(self, app: ASGIApp, *, upload_limit: Callable[[], int], json_limit: int = JSON_LIMIT,
                 prefix: str = "/api/") -> None:
        self.app = app
        self.upload_limit = upload_limit  # callable: follows config.json -> app.max_upload_mb
        self.json_limit = json_limit
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] != "http" or scope.get("method", "GET") in SAFE_METHODS
                or not scope.get("path", "").startswith(self.prefix)):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        multipart = headers.get("content-type", "").lower().startswith("multipart/form-data")
        limit = self.upload_limit() if multipart else self.json_limit
        declared = headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            await JSONResponse(_too_large(limit), status_code=413, headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        received = 0

        async def counted_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # FastAPI re-raises HTTPException from body parsing -> a normal 413 JSON response
                    raise HTTPException(status_code=413, detail=_too_large(limit)["detail"])
            return message

        await self.app(scope, counted_receive, send)
