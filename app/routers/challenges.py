"""Qwen-coder challenges: generate, submit, bug-hunt own Build code, reveal (next step)."""
from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Challenge, HiddenBug, User, utcnow
from ..security.auth import current_user
from ..services.progression import normalize
from ..workers.queue import queue
from .deps import bughunt_in_progress, challenge_out, require_ai, spam_gate

router = APIRouter(prefix="/api/challenges", tags=["challenges"])
LANGUAGES = {"python", "javascript", "typescript", "java", "c", "cpp", "csharp", "go", "rust", "sql", "php", "kotlin", "swift", "ruby"}


async def _own(session: AsyncSession, user: User, challenge_id: str) -> Challenge:
    try:
        cid = uuid.UUID(challenge_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Challenge not found.") from exc
    ch = await session.get(Challenge, cid)
    if ch is None or ch.user_id != user.id:
        raise HTTPException(status_code=404, detail="Challenge not found.")
    return ch


async def _bugs(session: AsyncSession, ch: Challenge) -> list[HiddenBug]:
    return list((await session.execute(select(HiddenBug).where(HiddenBug.challenge_id == ch.id).order_by(HiddenBug.created_at))).scalars())


class CreateIn(BaseModel):
    kind: Literal["bug_hunt", "why", "predict", "build"]
    topic: str = Field(default="", max_length=200)
    language: str = Field(default="python", max_length=40)


@router.post("")
async def create(body: CreateIn, request: Request, user: User = Depends(current_user),
                 session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_ai(user)
    await spam_gate(request, session, user, body.topic or f"new {body.kind} challenge", "challenge")
    # row lock so simultaneous clicks cannot slip past the "3 at a time" limit together
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    active = int((await session.execute(select(func.count()).select_from(Challenge).where(
        Challenge.user_id == user.id, Challenge.status.in_(("generating", "evaluating"))))).scalar_one())
    if active >= 3:
        raise HTTPException(status_code=409, detail="Three challenges are already being prepared - give the AI a moment.")
    language = body.language.lower().strip()
    ch = Challenge(user_id=user.id, kind=body.kind, level=normalize(user.progression)["level"],
                   language=language if language in LANGUAGES else "python", status="generating",
                   title="Generating...", meta={"topic": body.topic.strip()})
    session.add(ch)
    await session.flush()
    job = await queue.enqueue(session, kind="challenge_generate", user_id=user.id,
                              payload={"challenge_id": str(ch.id), "topic": body.topic.strip()},
                              label=f"Qwen coder: generate {body.kind.replace('_', ' ')} challenge", priority=2)
    await session.commit()
    await queue.notify()
    return {"challenge": challenge_out(ch, []), "job_id": job.id}


@router.get("")
async def list_challenges(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = list((await session.execute(select(Challenge).where(Challenge.user_id == user.id)
                                       .order_by(Challenge.created_at.desc()).limit(60))).scalars())
    return {"challenges": [challenge_out(ch, await _bugs(session, ch)) for ch in rows]}


@router.get("/{challenge_id}")
async def get_challenge(challenge_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ch = await _own(session, user, challenge_id)
    return challenge_out(ch, await _bugs(session, ch))


class SubmitIn(BaseModel):
    answer: str = Field(min_length=1, max_length=30000)
    confidence: int | None = Field(default=None, ge=0, le=100)


@router.post("/{challenge_id}/submit")
async def submit(challenge_id: str, body: SubmitIn, request: Request, user: User = Depends(current_user),
                 session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ch = await _own(session, user, challenge_id)
    if ch.status != "open":
        raise HTTPException(status_code=409, detail=f"This challenge is '{ch.status}', not open for answers.")
    require_ai(user)
    await spam_gate(request, session, user, body.answer.strip(), "challenge", max_chars=30_000)  # code answers are long
    # re-read under a row lock: of two submissions at the same moment only the first one is evaluated
    ch = await session.get(Challenge, ch.id, with_for_update=True, populate_existing=True)
    if ch is None or ch.status != "open":
        raise HTTPException(status_code=409, detail="This answer is already being evaluated.")
    ch.submission = body.answer.strip()
    ch.status = "evaluating"
    ch.meta = {**dict(ch.meta or {}), "error": None, "self_confidence": body.confidence}
    job = await queue.enqueue(session, kind="challenge_evaluate", user_id=user.id,
                              payload={"challenge_id": str(ch.id), "confidence": body.confidence},
                              label=f"Qwen coder: evaluate '{ch.title[:60]}'", priority=3)
    await session.commit()
    await queue.notify()
    return {"job_id": job.id}


class GuessIn(BaseModel):
    guesses: list[str] = Field(min_length=1, max_length=10)
    confidence: int | None = Field(default=None, ge=0, le=100)


@router.post("/{challenge_id}/bughunt")
async def bughunt(challenge_id: str, body: GuessIn, request: Request, user: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_ai(user)
    ch = await _own(session, user, challenge_id)
    ids = set((ch.secret or {}).get("submission_bug_ids") or [])
    hidden = [b for b in await _bugs(session, ch) if str(b.id) in ids and b.status == "hidden"]
    if not hidden:
        raise HTTPException(status_code=409, detail="No hidden bugs left in your submission.")
    guesses = [g.strip()[:800] for g in body.guesses if g.strip()]
    await spam_gate(request, session, user, "\n".join(guesses) or "guess", "chat", max_chars=9_000)  # 10 x 800
    if await bughunt_in_progress(session, user, "challenge_id", ch.id):
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": "Your previous guesses are still being checked."})
    job = await queue.enqueue(session, kind="bug_guess_eval", user_id=user.id,
                              payload={"challenge_id": str(ch.id), "guesses": guesses, "confidence": body.confidence},
                              label=f"Bug hunt on your '{ch.title[:50]}' submission", priority=3)
    await session.commit()
    await queue.notify()
    return {"job_id": job.id}


class RevealIn(BaseModel):
    give_up: bool = False


@router.post("/{challenge_id}/reveal")
async def reveal(challenge_id: str, body: RevealIn, user: User = Depends(current_user),
                 session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ch = await _own(session, user, challenge_id)
    if ch.status == "open" and not body.give_up:
        raise HTTPException(status_code=409, detail="Submit an answer first (or explicitly give up) - the reveal is the next step.")
    if ch.status not in ("open", "evaluated", "revealed"):
        raise HTTPException(status_code=409, detail=f"Cannot reveal while the challenge is '{ch.status}'.")
    if ch.status == "open":
        ch.score = 0.0
        ch.evaluation = {"score": 0, "feedback": "Gave up - solution revealed. No progress earned, but studying it still helps!",
                         "found_items": [], "total_items": (ch.meta or {}).get("items")}
    bugs = await _bugs(session, ch)
    for bug in bugs:
        if bug.status == "hidden":
            bug.status = "revealed"
            bug.resolved_at = utcnow()
    ch.status = "revealed"
    await session.commit()
    return challenge_out(ch, bugs)


@router.delete("/{challenge_id}")
async def delete_challenge(challenge_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ch = await _own(session, user, challenge_id)
    if ch.status in ("generating", "evaluating"):
        raise HTTPException(status_code=409, detail="Wait for the AI to finish first.")
    await session.execute(delete(Challenge).where(Challenge.id == ch.id))
    await session.commit()
    return {"deleted": True}
