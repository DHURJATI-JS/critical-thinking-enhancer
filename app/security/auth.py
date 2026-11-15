"""Local accounts and sessions.

* Every student has a username + password (scrypt hash) stored only in the local PostgreSQL.
* Each signed-in device gets its own random session token in an HttpOnly cookie; only its SHA-256
  is stored (``sessions`` table), so signing out on one device never affects another.
* The one-time recovery key (SHA-256 in ``users.token_hash``) can sign in when a password is
  forgotten; cookies from older versions that used the recovery key directly still work.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import User, UserSession

COOKIE_NAME = "cte_session"
SESSION_DAYS = 30
COOKIE_MAX_AGE = 60 * 60 * 24 * SESSION_DAYS


def new_token() -> str:
    return "cte_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def set_session_cookie(response: Response, token: str, request: Request | None = None) -> None:
    # Secure whenever the app is served over HTTPS (Talisman's session_cookie_secure); plain-HTTP LANs still work
    secure = request is not None and request.url.scheme == "https"
    response.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", secure=secure, path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


async def create_session(session: AsyncSession, user: User, request: Request, kind: str) -> str:
    """Create a device session and return its raw token (to be placed in the cookie)."""
    token = "cts_" + secrets.token_urlsafe(32)
    session.add(UserSession(user_id=user.id, token_hash=hash_token(token), kind=kind,
                            user_agent=(request.headers.get("user-agent") or "")[:200], ip=client_ip(request)))
    return token


async def end_session(session: AsyncSession, request: Request) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        await session.execute(delete(UserSession).where(UserSession.token_hash == hash_token(token)))


async def _lookup(request: Request, session: AsyncSession) -> User | None:
    token = request.cookies.get(COOKIE_NAME)
    request.state.cte_session = None
    if not token or len(token) > 200:
        return None
    digest = hash_token(token)
    now = datetime.now(timezone.utc)
    row = (await session.execute(select(UserSession).where(UserSession.token_hash == digest))).scalar_one_or_none()
    if row is not None:
        if row.last_seen_at and now - row.last_seen_at > timedelta(days=SESSION_DAYS):
            await session.delete(row)
            await session.commit()
            return None
        user = await session.get(User, row.user_id)
        request.state.cte_session = row
        if user is not None and (not row.last_seen_at or now - row.last_seen_at > timedelta(minutes=5)):
            row.last_seen_at = now
            user.last_seen_at = now
            await session.commit()
        return user
    # legacy cookie: the recovery key itself (profiles created before username/password login)
    user = (await session.execute(select(User).where(User.token_hash == digest))).scalar_one_or_none()
    if user is not None and (not user.last_seen_at or now - user.last_seen_at > timedelta(minutes=5)):
        user.last_seen_at = now
        await session.commit()
    return user


async def optional_user(request: Request, session: AsyncSession = Depends(get_session)) -> User | None:
    return await _lookup(request, session)


async def current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    """Signed-in user who is ALSO the one person currently using the app (single active user)."""
    from ..services import session_lock

    user = await _lookup(request, session)
    if user is None:
        raise HTTPException(status_code=401, detail="Please sign in (or create a profile) first.")
    # background polling (chat refresh, job status) keeps the lock alive but is not "using the app"
    background = request.headers.get("x-cte-background") == "1"
    ok, other = await session_lock.claim(session, user, interaction=not background)
    await session.commit()
    if not ok:
        view = session_lock.public_view(other)
        raise HTTPException(status_code=423, detail={
            "code": "APP_IN_USE", "holder": view.get("name"),
            "message": f"{view.get('name')} is using the app right now. You can continue when they finish.",
        })
    return user
