"""CSRF protection - signed double-submit token.

* Every browser gets a random ``cte_csrf`` cookie (HttpOnly, SameSite=Lax, Secure on HTTPS) the first time it
  talks to the app.
* Its token is ``HMAC-SHA256(server secret, cookie)``. The page receives it in ``<meta name="csrf-token">`` (or
  from ``GET /api/csrf``) and sends it back in the ``X-CSRF-Token`` header on every state-changing call.
* POST / PUT / PATCH / DELETE to ``/api/*`` without the right token -> ``403 CSRF_FAILED`` before any route code
  runs - this also covers login, sign-up and logout (login CSRF).

A page on another site can read neither the cookie nor the token, cannot compute the HMAC without the server
secret, and cannot even attach a custom header to a cross-site request without a CORS preflight, which this app
never grants. Two more layers run before the token check: the ``Origin`` header (when a browser sends one) must
be this app's own origin, and Fetch-Metadata ``Sec-Fetch-Site`` must be ``same-origin`` (or ``none``).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Sequence
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import derived_key

COOKIE_NAME = "cte_csrf"
HEADER_NAME = "X-CSRF-Token"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_COOKIE_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")


def token_for(cookie_value: str) -> str:
    digest = hmac.new(derived_key("csrf-v1"), cookie_value.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def valid_token(sent: str | None, cookie_value: str | None) -> bool:
    if not sent or not cookie_value or not _COOKIE_RE.fullmatch(cookie_value):
        return False
    return hmac.compare_digest(sent.encode("utf-8"), token_for(cookie_value).encode("ascii"))


def _netloc(value: str, scheme: str) -> str:
    netloc = value.strip().lower()
    default = {"http": ":80", "https": ":443"}.get(scheme)
    return netloc[: -len(default)] if default and netloc.endswith(default) else netloc


def same_origin(origin: str, host: str) -> bool:
    """Is the browser's Origin header this app's own origin (as addressed through the Host header)?"""
    parts = urlsplit(origin.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False  # includes "null" (sandboxed frames, opaque origins)
    return _netloc(parts.netloc, parts.scheme) == _netloc(host, parts.scheme)


def cookie_header(value: str, *, secure: bool) -> str:
    parts = [f"{COOKIE_NAME}={value}", "Path=/", f"Max-Age={COOKIE_MAX_AGE}", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


class CSRFMiddleware:
    """Pure-ASGI middleware: issues the cookie, exposes ``request.state.csrf_token`` and enforces the header."""

    def __init__(self, app: ASGIApp, *, protect_prefix: str = "/api/", exempt_paths: Sequence[str] = (),
                 trusted_origins: Sequence[str] = ()) -> None:
        self.app = app
        self.prefix = protect_prefix
        self.exempt = frozenset(exempt_paths)
        self.trusted_origins = frozenset(o.strip().rstrip("/").lower() for o in trusted_origins if o and o.strip())

    def _cross_site(self, headers: Headers) -> bool:
        origin = headers.get("origin")
        if origin is not None and not same_origin(origin, headers.get("host", "")) \
                and origin.strip().rstrip("/").lower() not in self.trusted_origins:
            return True
        site = headers.get("sec-fetch-site")
        return site is not None and site.lower() not in ("same-origin", "none")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        value = cookie_parser(headers.get("cookie", "")).get(COOKIE_NAME, "")
        fresh: str | None = None
        if not _COOKIE_RE.fullmatch(value):
            value = fresh = secrets.token_urlsafe(32)
        scope.setdefault("state", {})["csrf_token"] = token_for(value)
        secure = scope.get("scheme") == "https"

        async def send_with_cookie(message: Message) -> None:
            if fresh is not None and message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("set-cookie", cookie_header(fresh, secure=secure))
            await send(message)

        path = scope.get("path", "")
        if scope.get("method", "GET") not in SAFE_METHODS and path.startswith(self.prefix) and path not in self.exempt:
            problem = None
            if self._cross_site(headers):
                problem = "Cross-site request blocked."
            elif fresh is not None or not valid_token(headers.get(HEADER_NAME), value):
                problem = "Security check failed (missing or invalid CSRF token). Reload the page and try again."
            if problem:
                response = JSONResponse({"detail": {"code": "CSRF_FAILED", "message": problem}},
                                        status_code=403, headers={"Cache-Control": "no-store"})
                await response(scope, receive, send_with_cookie)
                return
        await self.app(scope, receive, send_with_cookie)
