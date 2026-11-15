"""Chat + level progression endpoints."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.router import role_budget
from ..config import UPLOAD_DIR, get_settings
from ..db import get_session
from ..models import Job, Message, User
from ..security.auth import current_user
from ..security.spam import guard
from ..ocr.pipeline import image_problem
from ..services.ingest import IMAGE_EXTS
from ..services.levels import LEVELS, get_level, level_public
from ..services.progression import normalize, respond_to_offer, set_level, upgrade
from ..workers.common import level_up_text, load_user, locked_text, system_message
from ..workers.queue import queue
from .deps import message_out, require_ai, spam_gate

router = APIRouter(prefix="/api", tags=["chat"])


def progression_out(user: User) -> dict[str, Any]:
    prog = normalize(user.progression)
    level = get_level(prog["level"])
    return {**prog, "level_info": level_public(level, role_budget("gateway")),
            "max_declines": get_settings().levels.max_declines_before_lock}


@router.get("/chat/messages")
async def list_messages(after_id: int = 0, limit: int = 80, user: User = Depends(current_user),
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    limit = max(1, min(200, limit))
    if after_id:
        rows = (await session.execute(select(Message).where(Message.user_id == user.id, Message.id > after_id)
                                      .order_by(Message.id.asc()).limit(limit))).scalars().all()
    else:
        rows = list(reversed((await session.execute(select(Message).where(Message.user_id == user.id)
                                                    .order_by(Message.id.desc()).limit(limit))).scalars().all()))
    pending = (await session.execute(select(Job.id).where(Job.user_id == user.id, Job.kind == "chat_turn",
                                                          Job.status.in_(("queued", "running"))))).scalars().first()
    return {"messages": [message_out(m) for m in rows], "progression": progression_out(user), "pending_job": pending}


class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


@router.post("/chat/send")
async def send(body: SendIn, request: Request, user: User = Depends(current_user),
               session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    text = body.text.strip()
    prog = normalize(user.progression)
    if prog["locked"]:
        raise HTTPException(status_code=409, detail={"code": "LEVEL_LOCKED",
                                                      "message": locked_text(get_level(prog["level"]), prog["declines"])})
    require_ai(user)
    spam = await spam_gate(request, session, user, text, "chat")
    # row lock: two sends at the same moment (double click, two tabs) cannot both start a turn
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    busy = (await session.execute(select(Job.id).where(Job.user_id == user.id, Job.kind == "chat_turn",
                                                       Job.status.in_(("queued", "running"))))).scalars().first()
    if busy:
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": "I'm still thinking about your previous message."})
    message = Message(user_id=user.id, role="user", content=text,
                      meta={"spam_score": spam["score"], "injection_suspected": spam["injection"], "level": prog["level"]})
    session.add(message)
    await session.flush()
    job = await queue.enqueue(session, kind="chat_turn", user_id=user.id,
                              payload={"message_id": message.id, "spam_check": spam["ai_check"]},
                              label="Chat turn: tutor + evaluator + Qwen code check (+ spam check) in parallel", priority=3)
    await session.commit()
    await queue.notify()
    return {"message": message_out(message), "job_id": job.id}


@router.post("/chat/ocr")
async def chat_ocr(request: Request, file: UploadFile = File(...), user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_ai(user)
    await guard.check_rate(str(user.id), "upload")
    ext = PurePosixPath((file.filename or "").lower()).suffix
    if ext not in IMAGE_EXTS:
        raise HTTPException(status_code=415, detail="Please upload an image (png, jpg, webp, bmp, tiff, gif).")
    data = await file.read(8_000_001)
    if len(data) > 8_000_000:
        raise HTTPException(status_code=413, detail="Image too large (max 8 MB).")
    problem = await asyncio.to_thread(image_problem, data)  # format allow-list + decompression-bomb guard
    if problem:
        raise HTTPException(status_code=415, detail=f"That image can't be read: {problem}.")
    path = UPLOAD_DIR / f"ocr_{uuid.uuid4().hex}{ext}"
    await asyncio.to_thread(path.write_bytes, data)
    job = await queue.enqueue(session, kind="ocr_image", user_id=user.id, payload={"path": str(path)},
                              label="OCR: Tesseract -> RapidOCR -> system OCR -> MiniCPM-V", priority=3)
    await session.commit()
    await queue.notify()
    return {"job_id": job.id}


@router.get("/progression")
async def get_progression(user: User = Depends(current_user)) -> dict[str, Any]:
    return progression_out(user)


class RespondIn(BaseModel):
    accept: bool


@router.post("/progression/respond")
async def respond(body: RespondIn, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    locked_user = await load_user(session, user.id, lock=True)
    prog, event = respond_to_offer(locked_user.progression, body.accept)
    if event == "no_offer":
        raise HTTPException(status_code=409, detail="There is no pending difficulty raise to answer.")
    locked_user.progression = prog
    level = get_level(prog["level"])
    max_declines = get_settings().levels.max_declines_before_lock
    if event == "level_up":
        msg = system_message(user.id, level_up_text(level), turn_type="level_up", mood="happy", level=level.number)
    elif event == "locked":
        msg = system_message(user.id, locked_text(level, prog["declines"]), turn_type="level_locked", mood="concerned",
                             level=level.number, actions=["upgrade"])
    else:
        msg = system_message(
            user.id,
            f"👍 No problem - we'll stay at **Level {level.number}**. I'll ask again after a few more exchanges. "
            f"({prog['declines']}/{max_declines} declines - after {max_declines} I'll consider this level cleared and an upgrade will be required.)",
            turn_type="level_declined", mood="neutral", level=level.number,
        )
    session.add(msg)
    await session.commit()
    return {"event": event, "progression": progression_out(locked_user), "message": message_out(msg)}


@router.get("/progression/levels")
async def list_levels(user: User = Depends(current_user)) -> dict[str, Any]:
    """All ten scrutiny levels (what the coach does at each one, sized for this device) + the current one."""
    budget = role_budget("gateway")
    return {"levels": [level_public(level, budget) for level in LEVELS.values()],
            "current": normalize(user.progression)["level"]}


class LevelIn(BaseModel):
    level: int = Field(ge=1, le=10)


@router.put("/progression/level")
async def change_level(body: LevelIn, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Choose the scrutiny level yourself - easier when it is too much, harder when you want a challenge."""
    await guard.check_rate(str(user.id), "level")
    locked_user = await load_user(session, user.id, lock=True)
    prog, event = set_level(locked_user.progression, body.level)
    if event == "same":
        return {"event": event, "progression": progression_out(locked_user), "message": None}
    locked_user.progression = prog
    level = get_level(prog["level"])
    lead = ("Scrutiny raised" if event == "raised" else "Scrutiny lowered - no problem, we'll take it step by step")
    msg = system_message(
        user.id,
        f"**{lead}: Level {level.number} - {level.name}** ({level.scrutiny}).\n\n"
        f"From now on: {level.rules}\n\nYour level bar starts again from 0 % at this level.",
        turn_type="level_change", mood="happy" if event == "raised" else "neutral", level=level.number,
    )
    session.add(msg)
    await session.commit()
    return {"event": event, "progression": progression_out(locked_user), "message": message_out(msg)}


@router.post("/progression/upgrade")
async def do_upgrade(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    locked_user = await load_user(session, user.id, lock=True)
    prog, event = upgrade(locked_user.progression)
    if event != "level_up":
        raise HTTPException(status_code=409, detail="Upgrade not available yet - fill the level bar first." if event == "not_ready"
                            else "You are already at the maximum level.")
    locked_user.progression = prog
    level = get_level(prog["level"])
    msg = system_message(user.id, level_up_text(level), turn_type="level_up", mood="happy", level=level.number)
    session.add(msg)
    await session.commit()
    return {"event": event, "progression": progression_out(locked_user), "message": message_out(msg)}
