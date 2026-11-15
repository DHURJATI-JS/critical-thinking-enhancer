"""SQLAlchemy 2.0 (async) models.

All free-form user details (name, age, gender, SWOT, biography, avatar/voice settings,
AI-derived cognitive data, progression state, encrypted API keys) are stored in
``MutableDict``-backed JSONB columns so in-place top-level changes are tracked and
persisted automatically. Use :func:`json_set` for nested updates.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import get_settings

EMBED_DIM = get_settings().embeddings.dim


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def json_set(container: MutableDict, key: str, value: Any) -> None:
    """Assign a (possibly nested) value so MutableDict registers the change."""
    container[key] = copy.deepcopy(value)


def json_merge(container: MutableDict, key: str, patch: dict[str, Any]) -> dict[str, Any]:
    current = copy.deepcopy(container.get(key) or {})
    current.update(patch)
    container[key] = current
    return current


class Base(DeclarativeBase):
    pass


def _json_dict() -> Any:
    return MutableDict.as_mutable(JSONB)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # SHA-256 of the one-time recovery key (also accepted as a legacy session cookie)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # login name (unique, case-insensitive - see the ux_users_username index in db.py) + scrypt password hash
    username: Mapped[str | None] = mapped_column(String(40), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str] = mapped_column(String(80))
    # name, age, gender, swot{strengths,weaknesses,opportunities,threats}, bio, project_notes
    profile: Mapped[dict] = mapped_column(_json_dict(), default=dict, nullable=False)
    # theme, avatar{face,hue,name}, voice{enabled,name,rate,pitch}, fallback{provider,enabled,...}
    settings: Mapped[dict] = mapped_column(_json_dict(), default=dict, nullable=False)
    # AI-derived: initial impressions, swot_analysis, narrative, ai_usage, ownership ...
    cognitive: Mapped[dict] = mapped_column(_json_dict(), default=dict, nullable=False)
    # level, progress, declines, offer_pending, locked, completed, history[], turns ...
    progression: Mapped[dict] = mapped_column(_json_dict(), default=dict, nullable=False)
    # Fernet-encrypted fallback API keys + validation status
    secrets: Mapped[dict] = mapped_column(_json_dict(), default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSession(Base):
    """One signed-in device. Only the SHA-256 of the random cookie token is stored."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="password")  # password | recovery | onboarding
    user_agent: Mapped[str] = mapped_column(String(200), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(24), default="upload")  # upload|paste|ocr|challenge|onboarding
    languages: Mapped[list] = mapped_column(MutableList.as_mutable(JSONB), default=list)
    files: Mapped[list] = mapped_column(MutableList.as_mutable(JSONB), default=list)
    content: Mapped[str] = mapped_column(Text, default="")
    # review, level_estimate, ai_detection, cognitive, ocr, bug_summary, discussed ...
    analysis: Mapped[dict] = mapped_column(_json_dict(), default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued")  # queued|analyzing|ready|failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectChunk(Base):
    __tablename__ = "project_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(400))
    idx: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    embed_model: Mapped[str] = mapped_column(String(120), default="")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), nullable=True)


class Challenge(Base):
    __tablename__ = "challenges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # bug_hunt|why|predict|build
    level: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(200), default="")
    language: Mapped[str] = mapped_column(String(40), default="python")
    prompt: Mapped[str] = mapped_column(Text, default="")
    code: Mapped[str] = mapped_column(Text, default="")
    # NEVER sent to the browser before reveal: planted bugs, expected answers, rubric, hidden edge cases
    secret: Mapped[dict] = mapped_column(_json_dict(), default=dict, nullable=False)
    meta: Mapped[dict] = mapped_column(_json_dict(), default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="generating")  # generating|open|evaluating|evaluated|revealed|failed
    submission: Mapped[str] = mapped_column(Text, default="")
    evaluation: Mapped[dict] = mapped_column(_json_dict(), default=dict, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HiddenBug(Base):
    __tablename__ = "hidden_bugs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    challenge_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("challenges.id", ondelete="CASCADE"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(240), default="")
    severity: Mapped[str] = mapped_column(String(16), default="medium")  # low|medium|high|critical
    category: Mapped[str] = mapped_column(String(40), default="logic")
    what_if: Mapped[str] = mapped_column(Text, default="")  # Socratic hint, safe to show
    fix: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    low_confidence: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="hidden")  # hidden|found|revealed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user|assistant|system
    content: Mapped[str] = mapped_column(Text)
    # mood, turn_type, confidence, low_confidence, level, provider, actions, eval ...
    meta: Mapped[dict] = mapped_column(_json_dict(), default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # answer|question|project|bio|swot|challenge|bug
    content: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str] = mapped_column(String(120), default="")
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    recall_count: Mapped[int] = mapped_column(Integer, default=0)
    last_recalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embed_model: Mapped[str] = mapped_column(String(120), default="")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Observation(Base):
    """One piece of evidence about a cognitive dimension (0-100) with a confidence weight."""

    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    dimension: Mapped[str] = mapped_column(String(32), index=True)
    score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))  # chat|project|challenge|bughunt|recall|calibration
    evidence: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    """Postgres-backed work queue row (claimed with FOR UPDATE SKIP LOCKED)."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|waiting|succeeded|failed
    priority: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str] = mapped_column(String(160), default="")
    payload: Mapped[dict] = mapped_column(_json_dict(), default=dict, nullable=False)
    result: Mapped[dict] = mapped_column(_json_dict(), default=dict, nullable=False)
    error: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    low_confidence: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True, index=True)
    pending_children: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str] = mapped_column(String(60), default="")
    # every job timestamp is taken from the DATABASE clock (now()), so durations never mix host and Docker clocks
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # refreshed every few seconds (database clock) by the process running the job: a job whose heartbeat stops
    # belongs to a crashed process and is recovered - a live job in another worker process never is
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_jobs_claim", "status", "priority", "id"),)


class SpamEvent(Base):
    __tablename__ = "spam_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(32))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(_json_dict(), default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# Approximate-nearest-neighbour indexes for pgvector (cosine distance).
Index(
    "ix_memories_embedding_hnsw",
    Memory.embedding,
    postgresql_using="hnsw",
    postgresql_with={"m": 16, "ef_construction": 64},
    postgresql_ops={"embedding": "vector_cosine_ops"},
)
Index(
    "ix_project_chunks_embedding_hnsw",
    ProjectChunk.embedding,
    postgresql_using="hnsw",
    postgresql_with={"m": 16, "ef_construction": 64},
    postgresql_ops={"embedding": "vector_cosine_ops"},
)
