"""Shared router helpers: AI availability gate, spam pipeline, safe serializers (never leak secrets)."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.router import can_run_now
from ..config import get_settings
from ..models import Challenge, HiddenBug, Message, Project, SpamEvent, User
from ..security.auth import client_ip
from ..security.spam import guard, heuristic_score


def require_ai(user: User | None) -> str:
    """503 (shown as the 'AI server stopped working' dialog) unless AI or the user's fallback is usable."""
    ok, route = can_run_now(user)
    if not ok:
        raise HTTPException(status_code=503, detail={"code": "AI_OFFLINE", "message": route})
    return route


async def spam_gate(request: Request, session: AsyncSession, user: User | None, text: str, kind: str,
                    *, max_chars: int | None = None) -> dict[str, Any]:
    """Rate limit + heuristics. Blocks obvious spam; returns {'ai_check': bool, ...} for the grey zone.
    ``max_chars`` overrides the chat-message limit for longer inputs (code answers, bug-hunt lists)."""
    cfg = get_settings().spam
    key = str(user.id) if user else client_ip(request)
    await guard.check_rate(key, kind)
    if user is not None:
        await guard.check_rate(f"ip:{client_ip(request)}", kind, multiplier=5)
    limit = max_chars or cfg.max_message_chars
    if len(text) > limit:
        raise HTTPException(status_code=413, detail=f"Message too long (max {limit} characters).")
    recent: list[str] = []
    if user is not None:
        recent = list((await session.execute(
            select(Message.content).where(Message.user_id == user.id, Message.role == "user").order_by(Message.id.desc()).limit(6)
        )).scalars())
    score, reasons, injection = heuristic_score(text, recent)
    if score >= cfg.block_threshold:
        muted = await guard.record_block(key)
        session.add(SpamEvent(user_id=user.id if user else None, ip=client_ip(request), kind=f"{kind}_heuristic",
                              score=score, reason="; ".join(reasons), blocked=True))
        await session.commit()
        raise HTTPException(status_code=422, detail={
            "code": "SPAM_BLOCKED",
            "message": "Spam protection blocked this message: " + "; ".join(reasons) + (" You are temporarily muted." if muted else ""),
        })
    return {"score": round(score, 2), "reasons": reasons, "injection": injection, "ai_check": score >= cfg.ai_check_threshold or injection}


async def bughunt_in_progress(session: AsyncSession, user: User, key: str, target_id: Any) -> bool:
    """Is a bug-hunt check for this project / challenge already queued or running? Takes the user's row lock
    first, so two clicks (or two tabs) at the same moment are decided one after the other."""
    from ..models import Job

    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    return (await session.execute(select(Job.id).where(
        Job.user_id == user.id, Job.kind == "bug_guess_eval", Job.status.in_(("queued", "running")),
        Job.payload[key].astext == str(target_id)).limit(1))).first() is not None


PUBLIC_META_DROP = {"recall_memory", "recall_memory_id"}


def message_out(m: Message) -> dict[str, Any]:
    meta = {k: v for k, v in (m.meta or {}).items() if k not in PUBLIC_META_DROP}
    if isinstance(meta.get("eval"), dict):
        meta["eval"] = {k: meta["eval"].get(k) for k in ("genuine", "dimensions", "low_confidence")}
    return {"id": m.id, "role": m.role, "content": m.content, "meta": meta, "created_at": m.created_at.isoformat() if m.created_at else None}


def bug_out(b: HiddenBug) -> dict[str, Any]:
    return {"id": str(b.id), "title": b.title, "description": b.description, "location": b.location, "severity": b.severity,
            "category": b.category, "fix": b.fix, "what_if": b.what_if, "status": b.status, "confidence": b.confidence,
            "low_confidence": b.low_confidence}


def _bug_block(bugs: list[HiddenBug]) -> dict[str, Any]:
    hidden = [b for b in bugs if b.status == "hidden"]
    by_sev: dict[str, int] = {}
    for b in hidden:
        by_sev[b.severity] = by_sev.get(b.severity, 0) + 1
    return {"total": len(bugs), "hidden_count": len(hidden), "hidden_by_severity": by_sev,
            "visible": [bug_out(b) for b in bugs if b.status != "hidden"]}


def project_out(p: Project, bugs: list[HiddenBug], *, detail: bool = False) -> dict[str, Any]:
    a = dict(p.analysis or {})
    out = {
        "id": str(p.id), "name": p.name, "description": p.description, "source": p.source, "languages": p.languages or [],
        "status": p.status, "created_at": p.created_at.isoformat() if p.created_at else None,
        "files": [{k: f.get(k) for k in ("path", "bytes", "kind")} for f in (p.files or []) if isinstance(f, dict)][:200],
        "analysis": {
            "review": a.get("review"), "ai_detection": a.get("ai_detection"), "ocr": a.get("ocr"), "error": a.get("error"),
            "cognitive": {"questions_to_ask": ((a.get("cognitive") or {}).get("questions_to_ask") or [])},
            "bug_meta": (a.get("bug_summary") or {}).get("meta"), "bughunt_attempts": a.get("bughunt_attempts") or [],
            "skipped": (a.get("skipped") or [])[:30], "group_job_id": a.get("group_job_id"), "chunks": a.get("chunks"),
        },
        "bugs": _bug_block(bugs),
    }
    if detail:
        out["content_preview"] = (p.content or "")[:20000]
    return out


def challenge_out(ch: Challenge, bugs: list[HiddenBug]) -> dict[str, Any]:
    meta = dict(ch.meta or {})
    out: dict[str, Any] = {
        "id": str(ch.id), "kind": ch.kind, "level": ch.level, "title": ch.title, "language": ch.language, "prompt": ch.prompt,
        "code": ch.code, "status": ch.status, "question": meta.get("question"), "skills": meta.get("skills") or [],
        "estimated_minutes": meta.get("estimated_minutes"), "items": meta.get("items"), "generation": meta.get("generation"),
        "error": meta.get("error"), "submission": ch.submission, "score": ch.score,
        "created_at": ch.created_at.isoformat() if ch.created_at else None,
        "evaluated_at": ch.evaluated_at.isoformat() if ch.evaluated_at else None,
    }
    if ch.evaluation:
        ev = dict(ch.evaluation)
        out["evaluation"] = {k: ev.get(k) for k in ("score", "feedback", "hints", "found_items", "total_items", "self_confidence",
                                                     "meta", "ai_detection", "ownership_questions", "hidden_bugs_in_submission")}
    submission_ids = set((ch.secret or {}).get("submission_bug_ids") or [])
    sub_bugs = [b for b in bugs if str(b.id) in submission_ids]
    out["submission_bugs"] = _bug_block(sub_bugs)
    if ch.status == "revealed":
        secret = dict(ch.secret or {})
        secret.pop("planted_bug_ids", None)
        secret.pop("submission_bug_ids", None)
        planted_status = {str(b.id): b.status for b in bugs}
        ids = (ch.secret or {}).get("planted_bug_ids") or []
        for i, bug in enumerate(secret.get("planted_bugs", [])):
            bug["found"] = i < len(ids) and planted_status.get(ids[i]) == "found"
        out["solution"] = secret
        out["submission_bugs"]["visible"] = [bug_out(b) for b in sub_bugs]
    return out
