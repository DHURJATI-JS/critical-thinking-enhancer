"""Projects: upload / paste / screenshots -> parallel AI analysis, secret bug hunt, reveal."""
from __future__ import annotations

import random
import shutil
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import UPLOAD_DIR
from ..db import get_session
from ..models import HiddenBug, Job, Project, ProjectChunk, User, utcnow
from ..security.auth import current_user
from ..services.levels import get_level
from ..services.progression import normalize
from ..services.uploads import create_project, read_uploads
from ..workers.common import system_message
from ..workers.queue import queue
from .deps import bughunt_in_progress, project_out, require_ai, spam_gate

router = APIRouter(prefix="/api/projects", tags=["projects"])


async def _own_project(session: AsyncSession, user: User, project_id: str) -> Project:
    try:
        pid = uuid.UUID(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    project = await session.get(Project, pid)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


async def _bugs(session: AsyncSession, project_id: uuid.UUID) -> list[HiddenBug]:
    return list((await session.execute(select(HiddenBug).where(HiddenBug.project_id == project_id)
                                       .order_by(HiddenBug.created_at))).scalars())


@router.get("")
async def list_projects(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    projects = list((await session.execute(select(Project).where(Project.user_id == user.id).order_by(Project.created_at.desc()))).scalars())
    return {"projects": [project_out(p, await _bugs(session, p.id)) for p in projects]}


@router.get("/{project_id}")
async def get_project(project_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    project = await _own_project(session, user, project_id)
    return project_out(project, await _bugs(session, project.id), detail=True)


@router.post("")
async def upload_project(
    request: Request,
    name: str = Form("", max_length=160),
    description: str = Form("", max_length=4000),
    pasted_code: str = Form("", max_length=400_000),
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_ai(user)
    await spam_gate(request, session, user, f"{name}\n{description}".strip() or "project upload", "upload")
    ingested = await read_uploads(files, pasted_code)
    project, job = await create_project(session, user, name=name, description=description, ingested=ingested,
                                        source="upload" if files else "paste", enqueue=True)
    if project is None:
        raise HTTPException(status_code=400, detail={"code": "EMPTY_UPLOAD", "message": "No readable code, text or images found.",
                                                     "skipped": ingested.skipped[:20]})
    await session.commit()
    await queue.notify()
    return {"project": project_out(project, []), "job_id": job.id if job else None}


@router.post("/{project_id}/analyze")
async def reanalyze(project_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_ai(user)
    project = await _own_project(session, user, project_id)
    if project.status in ("queued", "analyzing"):
        active = (await session.execute(select(Job.id).where(Job.user_id == user.id, Job.status.in_(("queued", "running", "waiting")),
                                                              Job.payload["project_id"].astext == str(project.id)))).scalars().first()
        if active:
            raise HTTPException(status_code=409, detail="This project is already being analysed.")
    await session.execute(delete(ProjectChunk).where(ProjectChunk.project_id == project.id))
    await session.execute(delete(HiddenBug).where(HiddenBug.project_id == project.id))
    project.analysis = {"skipped": (project.analysis or {}).get("skipped", []), "ocr": (project.analysis or {}).get("ocr", [])}
    project.status = "queued"
    job = await queue.enqueue(session, kind="project_ingest", user_id=user.id, payload={"project_id": str(project.id)},
                              label=f"Re-analyse '{project.name}'", priority=1)
    await session.commit()
    await queue.notify()
    return {"job_id": job.id}


class GuessIn(BaseModel):
    guesses: list[str] = Field(min_length=1, max_length=10)
    confidence: int | None = Field(default=None, ge=0, le=100)


@router.post("/{project_id}/bughunt")
async def bughunt(project_id: str, body: GuessIn, request: Request, user: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    require_ai(user)
    project = await _own_project(session, user, project_id)
    guesses = [g.strip()[:800] for g in body.guesses if g.strip()]
    if not guesses:
        raise HTTPException(status_code=400, detail="Write at least one hypothesis.")
    await spam_gate(request, session, user, "\n".join(guesses), "chat", max_chars=9_000)  # 10 x 800
    hidden = [b for b in await _bugs(session, project.id) if b.status == "hidden"]
    if not hidden:
        raise HTTPException(status_code=409, detail="No hidden bugs left in this project.")
    if await bughunt_in_progress(session, user, "project_id", project.id):
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": "Your previous guesses are still being checked."})
    job = await queue.enqueue(session, kind="bug_guess_eval", user_id=user.id,
                              payload={"project_id": str(project.id), "guesses": guesses, "confidence": body.confidence},
                              label=f"Bug hunt check on '{project.name}'", priority=3)
    await session.commit()
    await queue.notify()
    return {"job_id": job.id}


@router.get("/{project_id}/hint")
async def hint(project_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    project = await _own_project(session, user, project_id)
    level = get_level(normalize(user.progression)["level"])
    if level.number >= 7:
        raise HTTPException(status_code=403, detail=f"No hints at Level {level.number} ({level.scrutiny} scrutiny) - trust your what-ifs!")
    hidden = [b for b in await _bugs(session, project.id) if b.status == "hidden" and b.what_if]
    if not hidden:
        raise HTTPException(status_code=404, detail="No hints available.")
    return {"what_if": random.choice(hidden).what_if}


@router.post("/{project_id}/reveal")
async def reveal(project_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    project = await _own_project(session, user, project_id)
    bugs = await _bugs(session, project.id)
    hidden = [b for b in bugs if b.status == "hidden"]
    for bug in hidden:
        bug.status = "revealed"
        bug.resolved_at = utcnow()
    if hidden:
        session.add(system_message(user.id, f"🔓 Revealed **{len(hidden)}** remaining bug(s) in _{project.name}_. Study each fix - "
                                            "next time, ask yourself the what-if before the bug asks you.",
                                   turn_type="bug_reveal", mood="neutral", level=normalize(user.progression)["level"]))
    await session.commit()
    return project_out(project, bugs, detail=True)


@router.delete("/{project_id}")
async def delete_project(project_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    project = await _own_project(session, user, project_id)
    await session.execute(delete(Project).where(Project.id == project.id))
    await session.commit()
    shutil.rmtree(UPLOAD_DIR / str(project.id), ignore_errors=True)
    return {"deleted": True}
