"""Onboarding, local accounts (username + password, per-device sessions, recovery key), account deletion."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.router import can_run_now, role_budget
from ..config import UPLOAD_DIR, get_settings
from ..db import get_session
from ..models import Job, Project, User, UserSession
from ..security.auth import (
    clear_session_cookie,
    client_ip,
    create_session,
    current_user,
    end_session,
    hash_token,
    new_token,
    optional_user,
    set_session_cookie,
)
from ..security.passwords import (
    hash_password_async,
    normalize_username,
    password_problem,
    username_problem,
    verify_password_async,
)
from ..security.spam import guard, heuristic_score
from ..services import session_lock
from ..services.levels import get_level, level_public
from ..services.progression import default_progression, normalize
from ..services.tts import tts
from ..services.uploads import create_project, read_uploads
from ..workers.queue import queue

router = APIRouter(prefix="/api", tags=["account"])

GENDERS = {"female", "male", "non_binary", "prefer_not", "self_describe"}
FACES = {"bot", "ada", "neo", "kit"}
THEMES = {"dark", "light", "system"}


def user_summary(user: User) -> dict[str, Any]:
    prog = normalize(user.progression)
    level = get_level(prog["level"])
    return {
        "id": str(user.id),
        "name": user.display_name,
        "username": user.username,
        "has_password": bool(user.password_hash),
        "profile": dict(user.profile or {}),
        "settings": dict(user.settings or {}),
        "progression": prog,
        "level": level_public(level, role_budget("gateway")),
        "has_opening": bool((user.cognitive or {}).get("initial")),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


async def _username_taken(session: AsyncSession, username: str, exclude: User | None = None) -> bool:
    stmt = select(User.id).where(func.lower(User.username) == username)
    if exclude is not None:
        stmt = stmt.where(User.id != exclude.id)
    return (await session.execute(stmt)).first() is not None


@router.get("/me")
async def me(user: User = Depends(current_user)) -> dict[str, Any]:
    return user_summary(user)


def _in_use(other: dict[str, Any] | None) -> HTTPException:
    view = session_lock.public_view(other)
    return HTTPException(status_code=423, detail={
        "code": "APP_IN_USE", "holder": view.get("name"),
        "message": f"{view.get('name')} is using the app right now - only one person can use it at a time. Try again when they finish.",
    })


@router.get("/session")
async def session_info(request: Request, user: User | None = Depends(optional_user),
                       session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Like /me but never 401s. If someone ELSE is using the app, this device gets no profile data."""
    holder = await session_lock.holder(session)
    if user is not None and holder is not None and holder.get("user_id") != str(user.id):
        return {"user": None, "blocked": True, "lock": session_lock.public_view(holder)}
    row = getattr(request.state, "cte_session", None)
    return {"user": user_summary(user) if user else None, "lock": session_lock.public_view(holder),
            "session_kind": row.kind if row is not None else ("legacy" if user else None)}


@router.get("/auth/lock")
async def lock_status(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Is someone using the app right now? (first name only)"""
    return session_lock.public_view(await session_lock.holder(session))


@router.get("/auth/profiles")
async def profiles(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Family sign-in screen: who has a profile on this computer (public bits only - no private data)."""
    await guard.check_rate(f"ip:{client_ip(request)}", "keys", multiplier=10)
    # has_accounts lets a new device show "sign in" instead of "create a profile" even when nothing is listed
    has_accounts = (await session.execute(select(User.id).where(User.password_hash.is_not(None)).limit(1))).first() is not None
    if not get_settings().app.list_profiles_on_signin:
        return {"listed": False, "profiles": [], "has_accounts": has_accounts}
    rows = (await session.execute(
        select(User).where(User.username.is_not(None), User.password_hash.is_not(None)).order_by(User.last_seen_at.desc()).limit(24)
    )).scalars().all()
    out = []
    for u in rows:
        avatar = (u.settings or {}).get("avatar") or {}
        out.append({"username": u.username, "name": u.display_name, "face": avatar.get("face", "bot"), "hue": avatar.get("hue", 190)})
    return {"listed": True, "profiles": out, "has_accounts": has_accounts}


@router.get("/auth/username-available")
async def username_available(u: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    await guard.check_rate(f"ip:{client_ip(request)}", "keys", multiplier=5)
    username = normalize_username(u)
    problem = username_problem(username)
    if problem:
        return {"available": False, "problem": problem}
    taken = await _username_taken(session, username)
    return {"available": not taken, "problem": "That username is already taken on this computer." if taken else None}


@router.post("/onboarding")
async def onboarding(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    username: str = Form(..., min_length=3, max_length=32),
    password: str = Form(..., min_length=8, max_length=128),
    name: str = Form(..., min_length=1, max_length=60),
    age: int = Form(..., ge=8, le=100),
    gender: str = Form("prefer_not"),
    gender_custom: str = Form("", max_length=40),
    strengths: str = Form("", max_length=2000),
    weaknesses: str = Form("", max_length=2000),
    opportunities: str = Form("", max_length=2000),
    threats: str = Form("", max_length=2000),
    bio: str = Form("", max_length=6000),
    experience: str = Form("", max_length=300),
    start_level: int = Form(1, ge=1, le=10),
    tutor_name: str = Form("Byte", max_length=40),
    avatar_face: str = Form("bot"),
    avatar_hue: int = Form(190, ge=0, le=360),
    voice_name: str = Form("", max_length=200),
    voice_engine: str = Form("neural"),
    voice_neural: str = Form("", max_length=60),
    voice_rate: float = Form(1.0, ge=0.5, le=2.0),
    voice_pitch: float = Form(1.0, ge=0.0, le=2.0),
    voice_enabled: bool = Form(True),
    theme: str = Form("dark"),
    project_name: str = Form("", max_length=160),
    project_description: str = Form("", max_length=4000),
    pasted_code: str = Form("", max_length=400_000),
    website: str = Form(""),  # honeypot - humans never see this field
    form_elapsed_ms: int = Form(0),
    accepted_disclaimer: bool = Form(False),
    files: list[UploadFile] = File(default=[]),
) -> dict[str, Any]:
    await guard.check_rate(f"ip:{client_ip(request)}", "onboard")
    ok, other = await session_lock.can_start(session, None)
    if not ok:
        raise _in_use(other)
    if website.strip():
        raise HTTPException(status_code=400, detail="Invalid submission.")
    if form_elapsed_ms < 2500:
        raise HTTPException(status_code=400, detail="That was suspiciously fast - please fill in the form normally (bot protection).")
    if not accepted_disclaimer:
        raise HTTPException(status_code=400, detail="Please read and accept the disclaimer first.")
    uname = normalize_username(username)
    problem = username_problem(uname) or password_problem(password, uname)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    if await _username_taken(session, uname):
        raise HTTPException(status_code=409, detail="That username is already taken on this computer - pick another one.")
    if gender not in GENDERS:
        gender = "prefer_not"
    text_blob = "\n".join(t for t in (bio, strengths, weaknesses, opportunities, threats, project_description) if t.strip())
    if text_blob:  # all of these are optional: leaving them empty is not spam
        score, reasons, _ = heuristic_score(text_blob, [])
        if score >= get_settings().spam.block_threshold:
            raise HTTPException(status_code=422, detail={"code": "SPAM_BLOCKED", "message": "Spam protection: " + "; ".join(reasons)})

    ingested = await read_uploads(files, pasted_code)
    recovery_key = new_token()
    clean_name = name.strip()
    password_hash = await hash_password_async(password)
    user = User(
        token_hash=hash_token(recovery_key),
        username=uname,
        password_hash=password_hash,
        display_name=clean_name,
        profile={
            "name": clean_name,
            "age": age,
            "gender": gender,
            "gender_custom": gender_custom.strip() if gender == "self_describe" else "",
            "swot": {"strengths": strengths.strip(), "weaknesses": weaknesses.strip(),
                     "opportunities": opportunities.strip(), "threats": threats.strip()},
            "bio": bio.strip(),
            "experience": experience.strip(),
        },
        settings={
            "theme": theme if theme in THEMES else "dark",
            "avatar": {"face": avatar_face if avatar_face in FACES else "bot", "hue": avatar_hue, "name": tutor_name.strip() or "Byte"},
            "voice": {"enabled": voice_enabled, "engine": voice_engine if voice_engine in ("neural", "browser") else "neural",
                      "neural": voice_neural, "name": voice_name, "rate": voice_rate, "pitch": voice_pitch},
            "fallback": {"provider": "none", "enabled": True, "for_long_context": False},
        },
        cognitive={},
        progression=default_progression(start_level),
        secrets={},
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:  # two people grabbed the same username at the same moment
        await session.rollback()
        raise HTTPException(status_code=409, detail="That username was just taken - pick another one.") from exc
    ai_ok, _route = can_run_now(user)
    job_ids: list[int] = []
    project, job = await create_project(session, user, name=project_name, description=project_description,
                                        ingested=ingested, source="onboarding", enqueue=ai_ok)
    if job is not None:
        job_ids.append(job.id)
    if ai_ok:
        job = await queue.enqueue(session, kind="onboarding_analysis", user_id=user.id, payload={},
                                  label="Initial cognitive analysis + opening question", priority=2)
        job_ids.append(job.id)
    token = await create_session(session, user, request, "onboarding")
    ok, other = await session_lock.claim(session, user)
    if not ok:  # someone signed in during the few seconds this form was being saved
        await session.rollback()
        if project is not None:  # its screenshots were already written to disk
            shutil.rmtree(UPLOAD_DIR / str(project.id), ignore_errors=True)
        raise _in_use(other)
    await session.commit()
    if job_ids:
        await queue.notify()
    set_session_cookie(response, token, request)
    return {"user": user_summary(user), "recovery_key": recovery_key, "jobs": job_ids, "ai_online": ai_ok,
            "project_id": str(project.id) if project else None, "skipped": ingested.skipped[:20]}


@router.post("/onboarding/analyze")
async def rerun_onboarding(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    from .deps import require_ai

    require_ai(user)
    if (user.cognitive or {}).get("initial"):
        return {"job_id": None, "detail": "already analysed"}
    active = (await session.execute(select(Job.id).where(Job.user_id == user.id, Job.kind == "onboarding_analysis",
                                                          Job.status.in_(("queued", "running"))))).scalars().first()
    if active:
        return {"job_id": active}
    job = await queue.enqueue(session, kind="onboarding_analysis", user_id=user.id, payload={},
                              label="Initial cognitive analysis + opening question", priority=2)
    await session.commit()
    await queue.notify()
    return {"job_id": job.id}


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, response: Response, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    uname = normalize_username(body.username)
    ip = client_ip(request)
    # brute-force limits: 10 tries / 10 min per username from one device (so another device on the network cannot
    # lock the real owner out), 50 per username overall, 40 per device overall
    await guard.check_rate(f"login:{uname}:{ip}", "login")
    await guard.check_rate(f"login:{uname}", "login", multiplier=5)
    await guard.check_rate(f"ip:{ip}", "login", multiplier=4)
    user = (await session.execute(select(User).where(func.lower(User.username) == uname))).scalar_one_or_none()
    # one person at a time: while someone else is active nobody can even try a password
    free, other = await session_lock.can_start(session, user.id if user else None)
    if not free:
        raise _in_use(other)
    # verify even when the user does not exist so timing does not reveal which usernames exist
    ok = await verify_password_async(body.password, user.password_hash if user else None)
    if user is None or not ok:
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    token = await create_session(session, user, request, "password")
    claimed, other = await session_lock.claim(session, user)
    if not claimed:
        await session.rollback()
        raise _in_use(other)
    await session.commit()
    set_session_cookie(response, token, request)
    return {"user": user_summary(user)}


class RecoverIn(BaseModel):
    key: str = Field(min_length=10, max_length=200)


@router.post("/auth/recover")
async def recover(body: RecoverIn, request: Request, response: Response, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    await guard.check_rate(f"ip:{client_ip(request)}", "keys")
    user = (await session.execute(select(User).where(User.token_hash == hash_token(body.key.strip())))).scalar_one_or_none()
    free, other = await session_lock.can_start(session, user.id if user else None)
    if not free:
        raise _in_use(other)
    if user is None:
        raise HTTPException(status_code=404, detail="No profile matches that recovery key.")
    token = await create_session(session, user, request, "recovery")
    claimed, other = await session_lock.claim(session, user)
    if not claimed:
        await session.rollback()
        raise _in_use(other)
    await session.commit()
    set_session_cookie(response, token, request)
    return {"user": user_summary(user), "can_reset_password": True}


class PasswordIn(BaseModel):
    current_password: str = Field(default="", max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ConfirmIn(BaseModel):
    current_password: str = Field(default="", max_length=128)


async def confirm_password(request: Request, user: User, password: str) -> None:
    """Sensitive actions need the current password, so a browser someone left signed in cannot be used to take
    the profile over. Exception: this device just proved the recovery key (forgotten-password flow)."""
    row = getattr(request.state, "cte_session", None)
    if not user.password_hash or (row is not None and row.kind == "recovery"):
        return
    await guard.check_rate(f"pwcheck:{user.id}", "login")
    if not await verify_password_async(password, user.password_hash):
        raise HTTPException(status_code=403, detail={"code": "WRONG_PASSWORD", "message": "Your current password is not correct."})


@router.post("/auth/password")
async def change_password(body: PasswordIn, request: Request, user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = getattr(request.state, "cte_session", None)
    await confirm_password(request, user, body.current_password)
    problem = password_problem(body.new_password, user.username or "")
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    db_user = await session.get(User, user.id)
    db_user.password_hash = await hash_password_async(body.new_password)
    # a new password signs out every OTHER device
    stmt = delete(UserSession).where(UserSession.user_id == user.id)
    if row is not None:
        stmt = stmt.where(UserSession.id != row.id)
        row.kind = "password"
    await session.execute(stmt)
    await session.commit()
    return {"ok": True}


class SetupIn(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


@router.post("/auth/setup")
async def setup_credentials(body: SetupIn, request: Request, response: Response, user: User = Depends(current_user),
                            session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Profiles created before passwords existed choose a username + password once."""
    if user.username and user.password_hash:
        raise HTTPException(status_code=409, detail="This profile already has a username and password.")
    uname = normalize_username(body.username)
    problem = username_problem(uname) or password_problem(body.password, uname)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    if await _username_taken(session, uname, exclude=user):
        raise HTTPException(status_code=409, detail="That username is already taken on this computer.")
    db_user = await session.get(User, user.id)
    db_user.username = uname
    db_user.password_hash = await hash_password_async(body.password)
    token = await create_session(session, db_user, request, "password")  # move off the legacy recovery-key cookie
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="That username was just taken.") from exc
    set_session_cookie(response, token, request)
    return {"user": user_summary(db_user)}


@router.post("/auth/rotate")
async def rotate(body: ConfirmIn, request: Request, response: Response, user: User = Depends(current_user),
                 session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    await confirm_password(request, user, body.current_password)
    row = getattr(request.state, "cte_session", None)
    key = new_token()
    db_user = await session.get(User, user.id)
    db_user.token_hash = hash_token(key)
    await end_session(session, request)  # this device moves to a fresh session token (same kind)
    token = await create_session(session, db_user, request, row.kind if row is not None else "password")
    await session.commit()
    set_session_cookie(response, token, request)
    return {"recovery_key": key}


@router.post("/auth/logout")
async def logout(request: Request, response: Response, user: User | None = Depends(optional_user),
                 session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    await end_session(session, request)
    if user is not None:
        await session_lock.release(session, user.id)  # the next person can sign in right away
    await session.commit()
    clear_session_cookie(response)
    return {"ok": True}


@router.delete("/me")
async def delete_me(body: ConfirmIn, request: Request, response: Response, user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Delete the account for good: login, recovery key, sessions on every device, chats, projects + uploaded
    files, challenges, memories, cognitive profile, saved API keys and queued AI work. Needs the password."""
    await confirm_password(request, user, body.current_password)
    project_ids = list((await session.execute(select(Project.id).where(Project.user_id == user.id))).scalars())
    jobs = (await session.execute(select(Job.id, Job.kind, Job.payload).where(
        Job.user_id == user.id, Job.status.in_(("queued", "running", "waiting"))))).all()
    await session_lock.release(session, user.id)
    # every table that belongs to the account references users.id with ON DELETE CASCADE
    await session.execute(delete(User).where(User.id == user.id))
    await session.commit()
    queue.cancel_jobs([j.id for j in jobs])  # stop AI work already running for this account
    for pid in project_ids:
        shutil.rmtree(UPLOAD_DIR / str(pid), ignore_errors=True)
    for job in jobs:  # temporary chat-OCR images waiting for their job
        path = str((job.payload or {}).get("path") or "") if job.kind == "ocr_image" else ""
        if path:
            target = Path(path).resolve()
            if UPLOAD_DIR.resolve() in target.parents:
                target.unlink(missing_ok=True)
    tts.clear_cache()  # synthesized speech of this person's chats is no longer kept in memory
    clear_session_cookie(response)
    return {"deleted": True}
