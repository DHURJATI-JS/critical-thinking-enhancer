"""Cognitive profile: estimated IQ (playful range), skills, AI-analysed SWOT, readiness."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Challenge, HiddenBug, Job, Message, Observation, Project, User, json_set
from ..security.auth import current_user
from ..security.spam import heuristic_score
from ..services.profile import summarize
from ..services.progression import normalize
from ..workers.queue import queue
from .deps import require_ai, spam_gate

router = APIRouter(prefix="/api/profile", tags=["profile"])
MIN_OBSERVATIONS_FOR_PICTURE = 8
MIN_DIMENSIONS_FOR_PICTURE = 3


@router.get("")
async def get_profile(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    observations = list((await session.execute(select(Observation).where(Observation.user_id == user.id))).scalars())
    summary = summarize(observations)
    recent = sorted(observations, key=lambda o: o.created_at, reverse=True)[:15]

    async def count(stmt) -> int:
        return int((await session.execute(stmt)).scalar_one())

    stats = {
        "messages": await count(select(func.count()).select_from(Message).where(Message.user_id == user.id, Message.role == "user")),
        "projects": await count(select(func.count()).select_from(Project).where(Project.user_id == user.id)),
        "challenges_done": await count(select(func.count()).select_from(Challenge).where(Challenge.user_id == user.id, Challenge.score.is_not(None))),
        "avg_challenge_score": (await session.execute(select(func.avg(Challenge.score)).where(Challenge.user_id == user.id, Challenge.score.is_not(None)))).scalar_one(),
        "bugs_found": await count(select(func.count()).select_from(HiddenBug).where(HiddenBug.user_id == user.id, HiddenBug.status == "found")),
        "bugs_total": await count(select(func.count()).select_from(HiddenBug).where(HiddenBug.user_id == user.id)),
    }
    projects = list((await session.execute(select(Project).where(Project.user_id == user.id))).scalars())
    ai_usage = [((p.analysis or {}).get("ai_detection") or {}).get("likelihood") for p in projects]
    ai_usage = [x for x in ai_usage if isinstance(x, (int, float))]
    levels = [((p.analysis or {}).get("review") or {}).get("level_estimate") for p in projects]
    levels = [x for x in levels if isinstance(x, (int, float))]
    cognitive = dict(user.cognitive or {})
    total = summary["total_observations"]
    with_evidence = sum(1 for d in summary["dimensions"].values() if d["status"] != "insufficient")
    ready = total >= MIN_OBSERVATIONS_FOR_PICTURE and with_evidence >= MIN_DIMENSIONS_FOR_PICTURE
    readiness = {
        "ready": ready,
        "observations": total,
        "dimensions_with_evidence": with_evidence,
        "needed": MIN_OBSERVATIONS_FOR_PICTURE,
        "message": (
            "your chats, projects and challenges now give a stable picture - it is still only an estimate."
            if ready else
            f"the AI will not draw conclusions from a few chats. {with_evidence}/{MIN_DIMENSIONS_FOR_PICTURE} skills have enough "
            "evidence so far - everything below is provisional and more AI chatting is required."
        ),
    }
    return {
        "user": {"name": user.display_name, "profile": dict(user.profile or {}), "progression": normalize(user.progression)},
        "summary": summary,
        "readiness": readiness,
        "recent_observations": [{"dimension": o.dimension, "score": o.score, "confidence": o.confidence, "source": o.source,
                                 "evidence": o.evidence, "at": o.created_at.isoformat()} for o in recent],
        "swot_self": (user.profile or {}).get("swot") or {},
        "swot_ai": cognitive.get("swot_analysis"),
        "swot_meta": cognitive.get("swot_meta"),
        "narrative": cognitive.get("narrative"),
        "focus_next": cognitive.get("focus_next") or [],
        "insufficient_areas": cognitive.get("insufficient_areas") or [],
        "initial": cognitive.get("initial"),
        "refreshed_at": cognitive.get("refreshed_at"),
        "refresh_job_id": await active_refresh(session, user.id),  # the page shows "re-analysing..." while set
        "stats": stats,
        "coding": {"project_level_avg": round(sum(levels) / len(levels), 1) if levels else None,
                   "ai_assistance_avg": round(sum(ai_usage) / len(ai_usage), 2) if ai_usage else None},
        "disclaimer": "Student-grade project. Estimates are for self-reflection only - not a medical, clinical or real IQ assessment.",
    }


class SwotIn(BaseModel):
    # exactly the four quadrants, each bounded - an open dict let one request push megabytes through the spam check
    strengths: str = Field(default="", max_length=2000)
    weaknesses: str = Field(default="", max_length=2000)
    opportunities: str = Field(default="", max_length=2000)
    threats: str = Field(default="", max_length=2000)


class ProfileIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    age: int | None = Field(default=None, ge=8, le=100)
    gender: str | None = Field(default=None, max_length=20)
    gender_custom: str | None = Field(default=None, max_length=40)
    bio: str | None = Field(default=None, max_length=6000)
    experience: str | None = Field(default=None, max_length=300)
    swot: SwotIn | None = None


@router.put("")
async def update_profile(body: ProfileIn, request: Request, user: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    texts = [body.bio or ""] + (list(body.swot.model_dump().values()) if body.swot else [])
    blob = "\n".join(t for t in texts if t.strip())
    if blob:  # an empty bio / SWOT is allowed (clearing a field is not spam)
        score, reasons, _ = heuristic_score(blob, [])
        if score >= 0.8:
            raise HTTPException(status_code=422, detail={"code": "SPAM_BLOCKED", "message": "Spam protection: " + "; ".join(reasons)})
    for key in ("name", "age", "gender", "gender_custom", "bio", "experience"):
        value = getattr(body, key)
        if value is not None:
            user.profile[key] = value.strip() if isinstance(value, str) else value
    if body.name:
        user.display_name = body.name.strip()
    if body.swot is not None:
        swot = {k: v.strip() for k, v in body.swot.model_dump().items()}
        json_set(user.profile, "swot", swot)
    await session.commit()
    return {"profile": dict(user.profile)}


async def active_refresh(session: AsyncSession, user_id: Any) -> int | None:
    """The profile re-analysis already queued or running for this person, if any."""
    return (await session.execute(select(Job.id).where(
        Job.user_id == user_id, Job.kind == "profile_refresh", Job.status.in_(("queued", "running"))
    ).order_by(Job.id.desc()).limit(1))).scalar_one_or_none()


@router.post("/refresh")
async def refresh(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_ai(user)
    # one re-analysis at a time: a second click (another tab, the automatic refresh) joins the running one
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    running = await active_refresh(session, user.id)
    if running is not None:
        await session.commit()
        return {"job_id": running, "already_running": True}
    await spam_gate(request, session, user, "profile refresh", "challenge")
    job = await queue.enqueue(session, kind="profile_refresh", user_id=user.id, payload={}, label="Refresh SWOT & profile narrative")
    await session.commit()
    await queue.notify()
    return {"job_id": job.id, "already_running": False}
