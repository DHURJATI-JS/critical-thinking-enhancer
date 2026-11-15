"""Talisman for FastAPI - HTTP security headers and a fresh Content-Security-Policy nonce for every request.

Flask-Talisman (https://github.com/wntrblm/flask-talisman) only works with Flask, so this is a small pure-ASGI
port of the same idea - and of its option names - for FastAPI / Starlette:

* **Content-Security-Policy with a per-request nonce** (``request.state.csp_nonce``). A script runs only when it
  carries this response's nonce; ``'strict-dynamic'`` lets those scripts load their own ES-module imports. Inline
  event handlers, ``javascript:`` URLs, ``eval``, plugins, ``<base>`` hijacking and framing are all refused, and
  ``<style>`` elements need the nonce too (style *attributes* stay allowed for meters and avatar colours).
* **Strict-Transport-Security** on HTTPS responses only (browsers ignore it on plain HTTP, where this LAN app
  normally runs) and an optional **force_https** redirect for deployments behind TLS.
* X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, Cross-Origin-Opener-Policy,
  Cross-Origin-Resource-Policy, X-Permitted-Cross-Domain-Policies, Origin-Agent-Cluster, X-DNS-Prefetch-Control
  and ``X-XSS-Protection: 0`` (the legacy XSS auditor did more harm than good; CSP replaces it).

Headers a route sets itself are never overwritten.
"""
from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence

from starlette.datastructures import URL, MutableHeaders
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_NONCE_SLOT = "\x00nonce\x00"

DEFAULT_CSP: dict[str, str | list[str]] = {
    "default-src": "'self'",
    "script-src": ["'self'", "'strict-dynamic'"],  # + the request's nonce
    "style-src": ["'self'", "'unsafe-inline'"],    # fallback for browsers without the two directives below
    "style-src-elem": "'self'",                    # + the request's nonce: no injected <style> blocks
    "style-src-attr": "'unsafe-inline'",           # style="width:42%" attributes (meters, avatar colour)
    "img-src": ["'self'", "data:", "blob:"],
    "font-src": ["'self'", "data:"],
    "media-src": ["'self'", "blob:"],              # the avatar's neural voice is played from blob: URLs
    "connect-src": "'self'",
    "object-src": "'none'",
    "base-uri": "'none'",
    "form-action": "'self'",
    "frame-ancestors": "'none'",
    "frame-src": "'none'",
    "worker-src": "'self'",
    "manifest-src": "'self'",
}
DEFAULT_NONCE_IN: tuple[str, ...] = ("script-src", "style-src-elem")

DEFAULT_PERMISSIONS_POLICY: dict[str, str] = {
    "accelerometer": "()", "autoplay": "(self)", "camera": "()", "display-capture": "()", "geolocation": "()",
    "gyroscope": "()", "magnetometer": "()", "microphone": "()", "midi": "()", "payment": "()", "usb": "()",
}


def new_nonce() -> str:
    """128+ bits from the OS CSPRNG, base64url (valid in a CSP nonce-source)."""
    return secrets.token_urlsafe(18)


class Talisman:
    """Pure-ASGI security-header middleware (``app.add_middleware(Talisman, ...)``)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        force_https: bool = False,
        force_https_permanent: bool = False,
        strict_transport_security: bool = True,
        strict_transport_security_max_age: int = 31_536_000,
        strict_transport_security_include_subdomains: bool = True,
        strict_transport_security_preload: bool = False,
        content_security_policy: Mapping[str, str | Sequence[str]] | None = None,
        content_security_policy_nonce_in: Sequence[str] = DEFAULT_NONCE_IN,
        content_security_policy_report_only: bool = False,
        content_security_policy_report_uri: str | None = None,
        content_security_policy_exempt: Sequence[str] = (),
        frame_options: str | None = "DENY",
        referrer_policy: str | None = "same-origin",
        permissions_policy: Mapping[str, str] | None = None,
        cross_origin_opener_policy: str | None = "same-origin",
        cross_origin_resource_policy: str | None = "same-origin",
    ) -> None:
        self.app = app
        self.force_https = force_https
        self.redirect_status = 301 if force_https_permanent else 302
        hsts = None
        if strict_transport_security:
            hsts = f"max-age={int(strict_transport_security_max_age)}"
            if strict_transport_security_include_subdomains:
                hsts += "; includeSubDomains"
            if strict_transport_security_preload:
                hsts += "; preload"
        self.hsts = hsts
        policy = dict(DEFAULT_CSP if content_security_policy is None else content_security_policy)
        parts = []
        for directive, sources in policy.items():
            values = [sources] if isinstance(sources, str) else list(sources)
            if directive in content_security_policy_nonce_in:
                values.append(f"'nonce-{_NONCE_SLOT}'")
            parts.append(" ".join([directive, *values]).strip())
        if content_security_policy_report_uri:
            parts.append(f"report-uri {content_security_policy_report_uri}")
        self.csp_template = "; ".join(parts)
        self.csp_header = "content-security-policy-report-only" if content_security_policy_report_only else "content-security-policy"
        self.csp_exempt = tuple(content_security_policy_exempt)
        permissions = DEFAULT_PERMISSIONS_POLICY if permissions_policy is None else permissions_policy
        static: dict[str, str] = {
            "x-content-type-options": "nosniff",
            "x-xss-protection": "0",
            "x-permitted-cross-domain-policies": "none",
            "x-dns-prefetch-control": "off",
            "origin-agent-cluster": "?1",
        }
        if frame_options:
            static["x-frame-options"] = frame_options
        if referrer_policy:
            static["referrer-policy"] = referrer_policy
        if permissions:
            static["permissions-policy"] = ", ".join(f"{k}={v}" for k, v in permissions.items())
        if cross_origin_opener_policy:
            static["cross-origin-opener-policy"] = cross_origin_opener_policy
        if cross_origin_resource_policy:
            static["cross-origin-resource-policy"] = cross_origin_resource_policy
        self.static_headers = static

    def csp_for(self, nonce: str) -> str:
        return self.csp_template.replace(_NONCE_SLOT, nonce)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = scope.get("scheme") == "https"
        if self.force_https and not https:
            response = RedirectResponse(str(URL(scope=scope).replace(scheme="https")), status_code=self.redirect_status)
            await response(scope, receive, send)
            return
        nonce = new_nonce()
        scope.setdefault("state", {})["csp_nonce"] = nonce
        path = scope.get("path", "")
        with_csp = not any(path.startswith(prefix) for prefix in self.csp_exempt)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.static_headers.items():
                    headers.setdefault(name, value)
                if with_csp:
                    headers.setdefault(self.csp_header, self.csp_for(nonce))
                if https and self.hsts:
                    headers.setdefault("strict-transport-security", self.hsts)
            await send(message)

        await self.app(scope, receive, send_with_headers)
