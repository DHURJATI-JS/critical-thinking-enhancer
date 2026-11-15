"""FastAPI application entry point.

Run:  python -m uvicorn app.main:app --host 0.0.0.0 --port 8010
(or use scripts/install.ps1 / scripts/install.sh which also set everything up).
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders

from . import __version__
from .ai.clients import close_all
from .ai.embeddings import get_embedder
from .ai.health import monitor
from .ai.providers import close_http
from .bus import bus
from .config import get_settings
from .db import SessionLocal, engine, init_db
from .services import session_lock
from .ocr.pipeline import system_ocr_probe
from .routers import account, challenges, chat, profile, projects, settings, system, tts as tts_router
from .security.csrf import CSRFMiddleware
from .security.hosts import TrustedHosts
from .security.limits import BodySizeLimit
from .security.talisman import Talisman
from .services.tts import tts
from .services.hardware import detect_hardware
from .workers import tasks  # noqa: F401 - registers job handlers
from .workers.queue import queue

BASE = Path(__file__).resolve().parent
STATIC_DIR = BASE / "static"
INDEX_HTML = BASE / "templates" / "index.html"
# The page is rendered per request: every response gets its own CSP nonce and this browser's CSRF token.
INDEX_TEMPLATE = INDEX_HTML.read_text(encoding="utf-8")
log = logging.getLogger("cte")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # health pings every 5 s would flood the log
    await init_db()
    async with SessionLocal() as s:
        await session_lock.ensure_row(s)
        await s.commit()
    await asyncio.to_thread(detect_hardware)
    background = [
        asyncio.create_task(asyncio.to_thread(system_ocr_probe), name="cte-ocr-probe"),
        asyncio.create_task(get_embedder(), name="cte-embedder-warmup"),
        asyncio.create_task(asyncio.to_thread(tts.warm), name="cte-tts-warmup"),
    ]
    await queue.recover_stale(startup=True)
    queue.attach_bus()
    await bus.start()
    monitor.add_outage_listener(queue.halt_all)
    await monitor.check_once()
    monitor.start()
    await queue.start()
    s = get_settings()
    log.info("Critical Thinking Enhancer %s ready on http://%s:%s (AI online: %s)", __version__, s.app.host, s.app.port, monitor.ai_online)
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        await queue.stop()
        await monitor.stop()
        await bus.stop()
        await close_all()
        await close_http()
        await engine.dispose()


# Swagger UI pulls its JS/CSS from a CDN, so the interactive API docs are opt-in (CTE_API_DOCS=1)
# to keep the default app 100 % local.
_DOCS = os.environ.get("CTE_API_DOCS") == "1"
app = FastAPI(title="Critical Thinking Enhancer", version=__version__, lifespan=lifespan,
              docs_url="/api/docs" if _DOCS else None, redoc_url=None, openapi_url="/api/openapi.json" if _DOCS else None)


class CacheRules:
    """Private API data is never cached; the app's own JS/CSS is always revalidated (304 when unchanged) so an
    update never mixes old and new files. Pure ASGI (a BaseHTTPMiddleware would wrap errors raised while reading
    the body - e.g. the 413 from BodySizeLimit - in an exception group and turn them into a generic 400)."""

    def __init__(self, app_: Any) -> None:
        self.app = app_

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")

        async def send_with_rules(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if path.startswith("/api/"):
                    headers.setdefault("cache-control", "no-store")
                elif path.startswith(("/static/js/", "/static/css/")):
                    headers["cache-control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_rules)


# Layers, outermost first (Starlette runs the middleware added LAST first):
#   Talisman (headers + CSP nonce) -> TrustedHosts (DNS rebinding) -> CSRF (origin + token) -> body size
#   -> cache rules -> routes
app.add_middleware(CacheRules)
_sec = get_settings().security
app.add_middleware(BodySizeLimit, upload_limit=lambda: (get_settings().app.max_upload_mb + 2) * 1024 * 1024)
app.add_middleware(CSRFMiddleware, protect_prefix="/api/", trusted_origins=_sec.trusted_origins)
app.add_middleware(TrustedHosts, allowed_hosts=_sec.allowed_hosts)
app.add_middleware(
    Talisman,
    force_https=_sec.force_https,
    strict_transport_security_max_age=_sec.hsts_max_age,
    strict_transport_security_include_subdomains=_sec.hsts_include_subdomains,
    content_security_policy_report_only=_sec.csp_report_only,
    content_security_policy_exempt=("/api/docs",),  # opt-in Swagger UI loads its JS from a CDN
    frame_options=_sec.frame_options,
    referrer_policy=_sec.referrer_policy,
)

for module in (system, account, chat, projects, challenges, profile, settings, tts_router):
    app.include_router(module.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    html = (INDEX_TEMPLATE.replace("__CSP_NONCE__", request.state.csp_nonce)
            .replace("__CSRF_TOKEN__", request.state.csrf_token))
    # never cached: a stored copy would carry an old nonce
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "img" / "logo.svg", media_type="image/svg+xml")
