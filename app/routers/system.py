"""Public config, health (pinged every 5 s by the UI), system status, levels, job tracking."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..ai.embeddings import active_embedder_name
from ..ai.health import monitor
from ..ai.router import fallback_ready, role_budget
from ..bus import bus
from ..config import blocked_model_reason, get_settings, public_config, safe_link
from ..db import get_session
from ..models import Job, User
from ..ocr.pipeline import engines_status
from ..security.auth import current_user, optional_user
from ..services.hardware import hardware, low_end_report
from ..services import session_lock
from ..services.levels import LEVELS, level_public
from ..services.progression import normalize
from ..workers.queue import job_timing, queue

router = APIRouter(prefix="/api", tags=["system"])
STARTED = time.time()


@router.get("/config")
async def get_config() -> dict[str, Any]:
    return {**public_config(), "version": __version__}


@router.get("/csrf")
async def csrf_token(request: Request) -> dict[str, str]:
    """This browser's CSRF token (the page also gets it in <meta name="csrf-token">).
    Send it as the X-CSRF-Token header on every POST / PUT / PATCH / DELETE."""
    return {"token": request.state.csrf_token, "header": "X-CSRF-Token"}


@router.get("/health")
async def health(user: User | None = Depends(optional_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    snap = monitor.snapshot()
    # The browser pings this every 5 s: for the person using the app it is the heartbeat that keeps their
    # single-user lock; for anyone else it says who is using the app right now.
    snap["blocked"] = False
    if user is not None:
        ok, other = await session_lock.claim(session, user, interaction=False)
        await session.commit()
        snap["lock"] = {"locked": False} if ok else session_lock.public_view(other)
        if not ok:
            snap["blocked"] = True  # THIS signed-in device must wait (the browser shows the lock screen only then)
            user = None  # this device gets no personal data while someone else is active
    else:
        snap["lock"] = session_lock.public_view(await session_lock.holder(session))
    fb = fallback_ready(user)
    snap["user"] = {
        "signed_in": user is not None,
        "fallback_ready": fb,
        "can_run": snap["ai_online"] or fb,
        "level": normalize(user.progression)["level"] if user else None,
    }
    snap["low_end"] = low_end_report(monitor.roles)
    return snap


def _ai_server_help() -> dict[str, Any]:
    s = get_settings()
    repo = safe_link(s.app.ai_server_repo)
    return {
        "repo": repo,
        "repo_configured": bool(repo),
        "steps": [
            "Download / clone the AI server repo" + (f": {repo}" if repo else "") +
            " (set app.ai_server_repo in config.json if this is empty).",
            "Start it and copy its OpenAI-compatible base URL, API key and model name into .env "
            "(CTE_GATEWAY_URL, CTE_GATEWAY_API_KEY, CTE_GATEWAY_MODEL).",
            "Start the Qwen coder with: docker compose --profile coder up -d  (the installer picks the right GPU setup).",
            "Locally-run Gemma models are NOT supported - use the AI server repo instead.",
        ],
        "blocked": {role: blocked_model_reason(s, role) for role in ("gateway", "coder", "vision")},
    }


@router.get("/system")
async def system_status(user: User | None = Depends(optional_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if user is not None:
        current = await session_lock.holder(session)
        if current is not None and current.get("user_id") != str(user.id):
            user = None  # someone else is using the app: this device sees no personal jobs
    gateway_budget = role_budget("gateway")
    return {
        "version": __version__,
        "uptime_seconds": int(time.time() - STARTED),
        "health": monitor.snapshot(),
        "hardware": hardware(),
        "low_end": low_end_report(monitor.roles),
        "budgets": {"gateway": gateway_budget, "coder": role_budget("coder")},
        "levels": [level_public(level, gateway_budget) for level in LEVELS.values()],
        "ocr": await asyncio.to_thread(engines_status),  # may run a PowerShell probe on first call
        "queue": await queue.stats(user.id if user else None),
        "embeddings": {"provider": get_settings().embeddings.provider, "active": active_embedder_name(), "dim": get_settings().embeddings.dim},
        "redis": bus.status(),
        "ai_server": _ai_server_help(),
        "confidence_threshold": get_settings().ai.confidence_threshold,
        "fallback_ready": fallback_ready(user),
    }


def _job_out(job: Job, now: Any, children: list[Job] | None = None) -> dict[str, Any]:
    out = {
        "id": job.id, "kind": job.kind, "label": job.label, "status": job.status, "error": job.error,
        "confidence": job.confidence, "low_confidence": job.low_confidence, "provider": job.provider,
        "result": dict(job.result or {}) if job.status == "succeeded" else {}, "parent_id": job.parent_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        **job_timing(job, now),  # running for / took / waiting (seconds, database clock)
    }
    if children is not None:
        out["children"] = [_job_out(c, now) for c in children]
    return out


async def _db_now(session: AsyncSession) -> Any:
    return (await session.execute(select(func.now()))).scalar_one()


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    job = await session.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="Job not found.")
    children = list((await session.execute(select(Job).where(Job.parent_id == job.id).order_by(Job.id))).scalars())
    return _job_out(job, await _db_now(session), children)


@router.get("/jobs")
async def list_jobs(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = list((await session.execute(select(Job).where(Job.user_id == user.id).order_by(Job.id.desc()).limit(30))).scalars())
    now = await _db_now(session)
    by_parent: dict[int, list[Job]] = {}
    for job in rows:
        if job.parent_id:
            by_parent.setdefault(job.parent_id, []).append(job)
    top = [j for j in rows if not j.parent_id or j.parent_id not in {r.id for r in rows}]
    return {"jobs": [_job_out(j, now, sorted(by_parent.get(j.id, []), key=lambda c: c.id) if j.id in by_parent else None) for j in top]}
