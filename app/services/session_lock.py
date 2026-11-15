"""Single active user.

Many profiles can exist (a family on one computer) but the app is used by ONE person at a time.
While someone is signed in and active, nobody else can sign in, create a profile or use the app.
The lock is released when that person signs out, when their browser stops checking in (tab closed:
no heartbeat for ``lock_heartbeat_seconds``) or after ``lock_idle_minutes`` without interaction.
The lock row lives in PostgreSQL and is taken with SELECT ... FOR UPDATE, so two people can never
grab it at the same moment. The same person may continue on another of their own devices.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import AppState, User

KEY = "active_user"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> bool:
    return get_settings().app.single_active_user


def _expired(value: dict[str, Any], now: datetime) -> bool:
    cfg = get_settings().app
    try:
        heartbeat = datetime.fromisoformat(value["heartbeat"])
        activity = datetime.fromisoformat(value["activity"])
    except (KeyError, TypeError, ValueError):
        return True
    return now - heartbeat > timedelta(seconds=cfg.lock_heartbeat_seconds) or now - activity > timedelta(minutes=cfg.lock_idle_minutes)


async def ensure_row(session: AsyncSession) -> None:
    await session.execute(text("INSERT INTO app_state (key, value, updated_at) VALUES (:k, '{}'::jsonb, now()) ON CONFLICT (key) DO NOTHING"),
                          {"k": KEY})


async def holder(session: AsyncSession) -> dict[str, Any] | None:
    """Who is using the app right now (None if nobody / lock expired)."""
    if not enabled():
        return None
    row = await session.get(AppState, KEY, populate_existing=True)
    value = dict(row.value or {}) if row is not None else {}
    if not value.get("user_id") or _expired(value, _now()):
        return None
    return value


def public_view(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {"locked": False}
    cfg = get_settings().app
    first = str(value.get("name") or "Someone").split()[0][:30]
    view = {"locked": True, "name": first, "since": value.get("since"),
            "idle_minutes": cfg.lock_idle_minutes, "heartbeat_seconds": cfg.lock_heartbeat_seconds}
    if cfg.list_profiles_on_signin and value.get("username"):
        view["username"] = value["username"]  # family mode: lets that person continue on another device
    return view


REFRESH_SECONDS = 10  # skip the row write when the holder's lock was refreshed this recently


async def claim(session: AsyncSession, user: User, *, interaction: bool = True) -> tuple[bool, dict[str, Any] | None]:
    """Take or refresh the lock for ``user``. Returns (ok, blocking_holder). The caller commits.

    ``interaction=False`` (the 5-second heartbeat and background polling) only keeps the holder's own lock
    alive: it never takes a free lock and never resets the idle timer, so a tab left open neither grabs
    the app nor keeps it forever.
    """
    if not enabled():
        return True, None
    now = _now()
    uid = str(user.id)
    # fast path (no row lock): the holder's lock is live and was refreshed moments ago
    peek = await session.get(AppState, KEY, populate_existing=True)
    current = dict(peek.value or {}) if peek is not None else {}
    if current.get("user_id") == uid and not _expired(current, now):
        stamp = current.get("activity" if interaction else "heartbeat")
        try:
            if stamp and now - datetime.fromisoformat(stamp) < timedelta(seconds=REFRESH_SECONDS):
                return True, None
        except ValueError:
            pass

    row = (await session.execute(select(AppState).where(AppState.key == KEY).with_for_update()
                                 .execution_options(populate_existing=True))).scalar_one_or_none()
    value = dict(row.value or {}) if row is not None else {}
    live = bool(value.get("user_id")) and not _expired(value, now)
    mine = live and value.get("user_id") == uid
    if live and not mine:
        return False, value
    if not interaction:
        if mine:
            row.value = {**value, "heartbeat": now.isoformat()}
        return True, None
    fresh = {
        "user_id": uid,
        "name": user.display_name,
        "username": user.username,
        "since": value.get("since") if mine else now.isoformat(),
        "heartbeat": now.isoformat(),
        "activity": now.isoformat(),
    }
    if row is None:
        session.add(AppState(key=KEY, value=fresh))
    else:
        row.value = fresh
    return True, None


async def can_start(session: AsyncSession, user_id: uuid.UUID | None) -> tuple[bool, dict[str, Any] | None]:
    """May this person sign in / create a profile right now?"""
    current = await holder(session)
    if current is None or (user_id is not None and current.get("user_id") == str(user_id)):
        return True, None
    return False, current


async def release(session: AsyncSession, user_id: uuid.UUID) -> None:
    row = (await session.execute(select(AppState).where(AppState.key == KEY).with_for_update()
                                 .execution_options(populate_existing=True))).scalar_one_or_none()
    if row is not None and (row.value or {}).get("user_id") == str(user_id):
        row.value = {}
