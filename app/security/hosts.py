"""Host-header allow-list - blocks DNS-rebinding attacks.

A malicious website can point its own domain at 127.0.0.1 or this PC's LAN address ("DNS rebinding") and then
talk to the app as if it were same-origin - it would even get its own CSRF cookie and token. Its requests still
carry the attacker's domain in the ``Host`` header, so the app only answers to:

* IP addresses (a rebinding attack can never make the browser send an IP literal for its domain),
* ``localhost`` / ``*.localhost``,
* this computer's own names (``DESKTOP-ABC``, ``desktop-abc.local``, its FQDN, ...),
* hosts listed in ``config.json -> security.allowed_hosts`` (``"*.example.lan"`` wildcards; ``"*"`` turns the check
  off, e.g. behind a reverse proxy you trust).
"""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Sequence

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def machine_names() -> frozenset[str]:
    names = {"localhost"}
    for raw in (socket.gethostname(), socket.getfqdn()):
        name = (raw or "").strip().lower().rstrip(".")
        if not name:
            continue
        names.add(name)
        base = name.split(".")[0]
        names.update({base, f"{base}.local", f"{base}.lan", f"{base}.home", f"{base}.internal"})
    return frozenset(names)


def split_host(value: str) -> str:
    """'Host' header value -> lower-case hostname without port / IPv6 brackets."""
    host = value.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end > 0 else ""
    if host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host.rstrip(".")


def host_allowed(value: str, names: frozenset[str], extra: frozenset[str]) -> bool:
    host = split_host(value)
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost") or host in names:
        return True
    if "*" in extra or host in extra:
        return True
    return any(pattern.startswith("*.") and host.endswith(pattern[1:]) for pattern in extra)


class TrustedHosts:
    def __init__(self, app: ASGIApp, *, allowed_hosts: Sequence[str] = ()) -> None:
        self.app = app
        self.extra = frozenset(h.strip().lower() for h in allowed_hosts if h and h.strip())
        self.names = machine_names()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":  # (the app has no websockets)
            host = Headers(scope=scope).get("host")
            # browsers always send Host; an HTTP/1.0 tool without it cannot be a rebinding attack
            if host is not None and not host_allowed(host, self.names, self.extra):
                await PlainTextResponse("Invalid Host header - add this name to config.json -> security.allowed_hosts.",
                                        status_code=400)(scope, receive, send)
                return
        await self.app(scope, receive, send)
