"""Async database engine / session management (SQLAlchemy 2.0 + asyncpg + pgvector)."""
from __future__ import annotations

import time
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings
from .models import Base

_settings = get_settings()

engine = create_async_engine(
    _settings.database.url,
    pool_size=_settings.database.pool_size,
    max_overflow=_settings.database.max_overflow,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# Idempotent schema upgrades for databases created by earlier versions (create_all never alters tables).
MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(40)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username ON users (lower(username)) WHERE username IS NOT NULL",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ",
]


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        for statement in MIGRATIONS:
            await conn.execute(text(statement))


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def db_ping() -> dict:
    started = time.perf_counter()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"ok": True, "latency_ms": int((time.perf_counter() - started) * 1000), "detail": "PostgreSQL + pgvector reachable"}
    except Exception as exc:  # pragma: no cover - depends on infra
        return {"ok": False, "latency_ms": None, "detail": f"Database unreachable: {exc.__class__.__name__}: {exc}"[:300]}
