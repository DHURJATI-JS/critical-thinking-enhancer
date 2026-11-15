"""Helpers shared by job handlers."""
from __future__ import annotations

import json
import math
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.prompts import DIMENSIONS
from ..models import Message, Observation, User
from ..services.levels import Level, get_level
from ..services.progression import add_progress

TUTOR_DEFAULT = "Byte"
PROFILE_REFRESH_EVERY = 8


def clamp(value: Any, lo: float = 0.0, hi: float = 100.0, default: float | None = None) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(x) or math.isinf(x):
        return default
    if hi == 100.0 and 0.0 < x <= 1.0 and isinstance(value, float):
        x *= 100.0  # model answered on a 0..1 scale
    return max(lo, min(hi, x))


def as_list(value: Any, limit: int = 20) -> list[Any]:
    if isinstance(value, list):
        return value[:limit]
    if value in (None, "", {}):
        return []
    return [value]


def as_str(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:limit]
    return json.dumps(value, ensure_ascii=False)[:limit]


def tutor_name(user: User) -> str:
    return str(((user.settings or {}).get("avatar") or {}).get("name") or TUTOR_DEFAULT)[:40]


def profile_for_ai(user: User) -> dict[str, Any]:
    p = dict(user.profile or {})
    gender = p.get("gender") or ""
    return {
        "name": p.get("name"),
        "age": p.get("age"),
        "gender": gender if gender and gender != "prefer_not" else "not shared",
        "biography": as_str(p.get("bio"), 2500),
        "self_reported_swot": p.get("swot") or {},
        "experience": p.get("experience") or "",
    }


async def load_user(session: AsyncSession, user_id: uuid.UUID, *, lock: bool = False) -> User:
    stmt = select(User).where(User.id == user_id)
    if lock:
        # populate_existing: a User already loaded in this session (e.g. by current_user) is refreshed with the
        # row as it is NOW under the lock - otherwise the stale copy would overwrite a job's newer progress
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise RuntimeError("user no longer exists")
    return user


def clip_words(text: str, limit: int) -> str:
    """Shorten at a word boundary with an ellipsis (no "...the reli" cut in the middle of a word)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "…"


def add_observations(
    session: AsyncSession,
    user_id: uuid.UUID,
    scores: Any,
    *,
    confidence: float,
    source: str,
    cap: float,
    evidence_prefix: str = "",
) -> int:
    """Insert dimension scores. ``cap`` limits the weight of a single piece of evidence."""
    count = 0
    if not isinstance(scores, dict):
        return 0
    for dim, entry in scores.items():
        if dim not in DIMENSIONS:
            continue
        if isinstance(entry, dict):
            score = clamp(entry.get("score"))
            evidence = clip_words(as_str(entry.get("evidence"), 4000), 420)
        else:
            score = clamp(entry)
            evidence = ""
        if score is None:
            continue
        session.add(
            Observation(
                user_id=user_id,
                dimension=dim,
                score=round(score, 1),
                confidence=round(max(0.05, min(cap, confidence)), 3),
                source=source,
                evidence=clip_words(evidence_prefix + evidence, 600),
            )
        )
        count += 1
    return count


def offer_text(level: Level) -> str:
    nxt = get_level(level.number + 1)
    return (
        f"**Level {level.number} bar complete!** You've been reasoning well.\n\n"
        f"Are you comfortable raising the difficulty to **Level {nxt.number} - {nxt.name}**? "
        f"Scrutiny becomes *{nxt.scrutiny}*, and I'll think harder and use more context memory "
        f"(about {nxt.context_tokens:,} tokens)."
    )


def locked_text(level: Level, declines: int) -> str:
    nxt = get_level(level.number + 1)
    return (
        f"You've declined the difficulty raise {declines} times, so I'm stopping here.\n\n"
        f"**Level {level.number} is cleared** - you've mastered it. To continue training, "
        f"**upgrading to Level {nxt.number} ({nxt.name}) is required.**"
    )


def level_up_text(level: Level) -> str:
    return f"🚀 **Welcome to Level {level.number} - {level.name}!** Scrutiny: *{level.scrutiny}*.\n\n{level.rules}"


def congrats_text(name: str) -> str:
    return (
        f"**Congratulations, {name}!** You've completed **Level 10 - Grandmaster**.\n\n"
        "That took persistence, honesty about what you didn't know yet, and a lot of careful what-if thinking. "
        "Thank you for all your hard work! You can keep practising at Level 10, revisit your Cognitive Profile, "
        "or start a fresh Build challenge."
    )


def system_message(user_id: uuid.UUID, content: str, *, turn_type: str, mood: str = "happy", level: int, **meta: Any) -> Message:
    return Message(
        user_id=user_id,
        role="assistant",
        content=content,
        meta={"turn_type": turn_type, "mood": mood, "level": level, "confidence": 1.0, "low_confidence": False, "rule_based": True, **meta},
    )


async def apply_progress(session: AsyncSession, user: User, gain: float) -> list[str]:
    """Add progress (user row must be locked). Posts level-offer / congratulation messages."""
    prog, events = add_progress(user.progression, gain)
    user.progression = prog
    level = get_level(prog["level"])
    if "offer" in events:
        session.add(system_message(user.id, offer_text(level), turn_type="level_offer", mood="proud", level=level.number,
                                   actions=["accept_raise", "decline_raise"]))
    if "completed" in events:
        name = (user.profile or {}).get("name") or user.display_name
        session.add(system_message(user.id, congrats_text(name), turn_type="congrats", mood="proud", level=10, celebrate=True))
    return events


async def observation_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    return int((await session.execute(select(func.count()).select_from(Observation).where(Observation.user_id == user_id))).scalar_one())


async def maybe_queue_profile_refresh(session: AsyncSession, user_id: uuid.UUID, before: int, after: int) -> bool:
    if after // PROFILE_REFRESH_EVERY > before // PROFILE_REFRESH_EVERY:
        from ..models import Job
        from .queue import queue

        busy = (await session.execute(select(Job.id).where(
            Job.user_id == user_id, Job.kind == "profile_refresh", Job.status.in_(("queued", "running"))).limit(1))).first()
        if busy is not None:  # never two profile re-analyses at once
            return False
        await queue.enqueue(session, kind="profile_refresh", user_id=user_id, payload={}, label="Refresh SWOT & profile narrative", priority=-1)
        return True
    return False
