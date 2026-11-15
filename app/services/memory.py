"""Long-term memory on pgvector: store, semantic recall and "do you remember...?" callbacks."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.embeddings import embed_texts
from ..models import Memory, ProjectChunk


async def remember(
    session: AsyncSession,
    user_id: uuid.UUID,
    kind: str,
    content: str,
    *,
    source_ref: str = "",
    importance: float = 0.5,
) -> Memory | None:
    content = (content or "").strip()
    if len(content) < 12:
        return None
    name, vectors = await embed_texts([content])
    memory = Memory(
        user_id=user_id,
        kind=kind,
        content=content[:4000],
        source_ref=source_ref[:120],
        importance=importance,
        embed_model=name,
        embedding=vectors[0],
    )
    session.add(memory)
    return memory


async def recall(session: AsyncSession, user_id: uuid.UUID, query: str, k: int) -> list[Memory]:
    if k <= 0 or not (query or "").strip():
        return []
    name, vectors = await embed_texts([query])
    stmt = (
        select(Memory)
        .where(Memory.user_id == user_id, Memory.embed_model == name, Memory.embedding.is_not(None))
        .order_by(Memory.embedding.cosine_distance(vectors[0]))
        .limit(k)
    )
    return list((await session.execute(stmt)).scalars())


async def pick_callback(session: AsyncSession, user_id: uuid.UUID, older_than_minutes: int = 15) -> Memory | None:
    """An older memory the student has rarely been asked about (for memory-recall questions)."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    stmt = (
        select(Memory)
        .where(
            Memory.user_id == user_id,
            Memory.kind.in_(["answer", "project", "challenge", "bug"]),
            Memory.created_at < cutoff,
        )
        .order_by(Memory.recall_count.asc(), Memory.importance.desc(), func.random())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def search_chunks(session: AsyncSession, user_id: uuid.UUID, query: str, k: int) -> list[ProjectChunk]:
    if k <= 0 or not (query or "").strip():
        return []
    name, vectors = await embed_texts([query])
    stmt = (
        select(ProjectChunk)
        .where(ProjectChunk.user_id == user_id, ProjectChunk.embed_model == name, ProjectChunk.embedding.is_not(None))
        .order_by(ProjectChunk.embedding.cosine_distance(vectors[0]))
        .limit(k)
    )
    return list((await session.execute(stmt)).scalars())
