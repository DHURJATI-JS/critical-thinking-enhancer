"""Talisman-style security headers, the per-request CSP nonce and CSRF tokens (no database needed)."""
from __future__ import annotations

import re

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.security import csrf as csrf_mod
from app.security.csrf import CSRFMiddleware
from app.security.talisman import Talisman


@pytest.fixture(autouse=True)
def _test_key(monkeypatch):
    monkeypatch.setattr(csrf_mod, "derived_key", lambda purpose: b"unit-test-key-" + purpose.encode())


def make_app(**talisman_kwargs) -> FastAPI:
    app = FastAPI()

    @app.get("/")
    async def page(request: Request) -> HTMLResponse:
        return HTMLResponse(f'<script nonce="{request.state.csp_nonce}" src="/a.js"></script>'
                            f'<meta name="csrf-token" content="{request.state.csrf_token}">')

    @app.get("/api/csrf")
    async def token(request: Request) -> dict:
        return {"token": request.state.csrf_token}

    @app.post("/api/thing")
    async def thing() -> dict:
        return {"ok": True}

    @app.get("/own-header")
    async def own() -> PlainTextResponse:
        return PlainTextResponse("x", headers={"X-Frame-Options": "SAMEORIGIN"})

    app.add_middleware(CSRFMiddleware)
    app.add_middleware(Talisman, **talisman_kwargs)
    return app


def client(app: FastAPI, base: str = "http://testserver") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base)


def nonce_of(csp: str) -> str:
    match = re.search(r"script-src [^;]*'nonce-([A-Za-z0-9_-]+)'", csp)
    assert match, csp
    return match.group(1)


async def test_csp_nonce_is_fresh_per_request_and_matches_the_page():
    async with client(make_app()) as c:
        first, second = await c.get("/"), await c.get("/")
    n1, n2 = nonce_of(first.headers["content-security-policy"]), nonce_of(second.headers["content-security-policy"])
    assert n1 != n2 and len(n1) >= 22
    assert f'nonce="{n1}"' in first.text and f'nonce="{n2}"' in second.text
    csp = first.headers["content-security-policy"]
    for directive in ("'strict-dynamic'", "object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'", "form-action 'self'"):
        assert directive in csp
    script_src = re.search(r"script-src ([^;]*)", csp).group(1)
    assert "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src
    assert f"'nonce-{n1}'" in re.search(r"style-src-elem ([^;]*)", csp).group(1)  # no injected <style> blocks either


async def test_talisman_headers_and_no_hsts_on_plain_http():
    async with client(make_app()) as c:
        r = await c.get("/")
    h = r.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "same-origin"
    assert "camera=()" in h["permissions-policy"] and "microphone=()" in h["permissions-policy"]
    assert h["cross-origin-opener-policy"] == "same-origin" and h["cross-origin-resource-policy"] == "same-origin"
    assert h["x-xss-protection"] == "0"
    assert "strict-transport-security" not in h  # plain-HTTP LAN: HSTS would be meaningless


async def test_hsts_only_over_https_and_force_https_redirect():
    async with client(make_app(), "https://testserver") as c:
        r = await c.get("/")
    assert r.headers["strict-transport-security"].startswith("max-age=31536000")
    async with client(make_app(force_https=True)) as c:
        r = await c.get("/page?x=1", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://testserver/page?x=1"


async def test_route_headers_win_and_report_only_mode():
    async with client(make_app(content_security_policy_report_only=True)) as c:
        own = await c.get("/own-header")
        page = await c.get("/")
    assert own.headers["x-frame-options"] == "SAMEORIGIN"
    assert "content-security-policy" not in page.headers and "content-security-policy-report-only" in page.headers


async def test_csrf_token_required_for_state_changes():
    app = make_app()
    async with client(app) as c:
        r = await c.post("/api/thing")
        assert r.status_code == 403 and r.json()["detail"]["code"] == "CSRF_FAILED"
        token = (await c.get("/api/csrf")).json()["token"]
        assert (await c.post("/api/thing", headers={"X-CSRF-Token": "forged" + token[6:]})).status_code == 403
        assert (await c.post("/api/thing", headers={"X-CSRF-Token": token})).status_code == 200
        page = await c.get("/")
        assert f'content="{token}"' in page.text  # same token in the page as from the endpoint
    async with client(app) as other:
        await other.get("/api/csrf")  # a different browser = a different cookie
        assert (await other.post("/api/thing", headers={"X-CSRF-Token": token})).status_code == 403


async def test_csrf_cookie_flags_and_rotation():
    async with client(make_app()) as c:
        r = await c.get("/api/csrf")
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("cte_csrf=") and "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" not in cookie
        again = await c.get("/api/csrf")
        assert "set-cookie" not in again.headers and again.json() == r.json()  # stable while the cookie exists
        c.cookies.clear()
        r2 = await c.post("/api/thing", headers={"X-CSRF-Token": r.json()["token"]})
        assert r2.status_code == 403 and "cte_csrf=" in r2.headers["set-cookie"]  # new cookie -> client refreshes
    async with client(make_app(), "https://testserver") as c:
        assert "Secure" in (await c.get("/api/csrf")).headers["set-cookie"]


async def test_safe_methods_and_non_api_paths_need_no_token():
    async with client(make_app()) as c:
        assert (await c.get("/api/csrf")).status_code == 200
        assert (await c.get("/")).status_code == 200
