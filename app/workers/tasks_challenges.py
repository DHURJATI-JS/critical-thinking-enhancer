"""Qwen-coder challenges: Bug Hunt, Why?, Predict and Build - generation and evaluation.

Secrets (planted bugs, model answers, expected output, hidden edge cases, reference solution,
bugs found in the student's own Build submission) are stored server-side only and revealed in
the next step.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from ..ai import prompts
from ..ai.gate import ask_json
from ..config import get_settings
from ..db import SessionLocal
from ..models import Challenge, HiddenBug, Message, Observation, Project, json_set, utcnow
from ..services.ai_detect import combine, heuristic_ai_likelihood
from ..services.levels import get_level
from ..services.memory import remember
from ..services.profile import calibration_score, summarize
from .common import add_observations, apply_progress, as_list, as_str, clamp, load_user, profile_for_ai
from .registry import JobContext, JobOutcome, job_handler, on_failure

SEVERITIES = ("low", "medium", "high", "critical")


def _bug(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict) or not str(item.get("title") or "").strip():
        return None
    severity = str(item.get("severity") or "medium").lower()
    return {
        "title": as_str(item.get("title"), 240),
        "location": as_str(item.get("location"), 240),
        "description": as_str(item.get("description"), 1500),
        "what_if": as_str(item.get("what_if"), 500),
        "fix": as_str(item.get("fix"), 1500),
        "severity": severity if severity in SEVERITIES else "medium",
    }


def secret_items(ch: Challenge) -> list[str]:
    secret = ch.secret or {}
    if ch.kind == "bug_hunt":
        return [f"{b['title']} @ {b.get('location', '')}: {b.get('description', '')}" for b in secret.get("planted_bugs", [])]
    if ch.kind == "predict":
        return [f"Expected output: {secret.get('expected_output', '')}"] + [str(r) for r in secret.get("rubric", [])]
    if ch.kind == "build":
        return [f"Edge case: {e}" for e in secret.get("hidden_edge_cases", [])] + [str(r) for r in secret.get("rubric", [])]
    return [str(r) for r in secret.get("rubric", [])]


@job_handler("challenge_generate")
async def challenge_generate(ctx: JobContext) -> JobOutcome:
    challenge_id = uuid.UUID(ctx.payload["challenge_id"])
    async with SessionLocal() as s:
        ch = await s.get(Challenge, challenge_id)
        if ch is None:
            raise RuntimeError("challenge no longer exists")
        user = await load_user(s, ctx.user_id)
        summary = summarize((await s.execute(select(Observation).where(Observation.user_id == user.id))).scalars().all())
        langs = sorted({lang for p in (await s.execute(select(Project).where(Project.user_id == user.id))).scalars() for lang in (p.languages or [])})
    level = get_level(ch.level)
    weak = [d["label"] for d in sorted(summary["dimensions"].values(), key=lambda d: d["score"]) if d["status"] != "insufficient"][:2]
    hint = (f"Weaker areas so far: {', '.join(weak) or 'not enough data yet'}; languages they use: {', '.join(langs) or 'unknown'}; "
            f"self-described experience: {as_str(profile_for_ai(user).get('experience'), 200) or 'n/a'}")
    required: tuple[str, ...] = ("title", "prompt")
    if ch.kind == "bug_hunt":
        required += ("code", "planted_bugs")
    elif ch.kind in ("why", "predict"):
        required += ("code", "question")
    gate = await ask_json(
        "coder", prompts.challenge_system(ch.kind),
        prompts.challenge_prompt(ch.kind, level, ctx.payload.get("topic") or "", ch.language, hint),
        user=user, required=required, temperature=0.7, max_tokens=1500 + 150 * level.number,
    )
    d = gate.data
    secret: dict[str, Any] = {}
    if ch.kind == "bug_hunt":
        secret["planted_bugs"] = [b for b in (_bug(x) for x in as_list(d.get("planted_bugs"), 8)) if b]
    elif ch.kind == "why":
        secret.update(rubric=[as_str(x, 300) for x in as_list(d.get("rubric"), 8)], model_answer=as_str(d.get("model_answer"), 3000))
    elif ch.kind == "predict":
        secret.update(expected_output=as_str(d.get("expected_output"), 2000), explanation=as_str(d.get("explanation"), 3000),
                      rubric=[as_str(x, 300) for x in as_list(d.get("rubric"), 8)])
    else:
        secret.update(hidden_edge_cases=[as_str(x, 300) for x in as_list(d.get("hidden_edge_cases"), 10)],
                      rubric=[as_str(x, 300) for x in as_list(d.get("rubric"), 8)],
                      reference_solution=as_str(d.get("reference_solution"), 8000))
    async with SessionLocal() as s:
        ch = await s.get(Challenge, challenge_id, with_for_update=True)
        if ch is None:
            raise RuntimeError("challenge deleted")
        ch.title = as_str(d.get("title"), 200) or f"{ch.kind.replace('_', ' ').title()} challenge"
        ch.language = as_str(d.get("language"), 40) or ch.language
        ch.prompt = as_str(d.get("prompt"), 6000)
        ch.code = as_str(d.get("code"), 12000)
        bug_ids = []
        for bug in secret.get("planted_bugs", []):
            row = HiddenBug(user_id=ch.user_id, challenge_id=ch.id, title=bug["title"], description=bug["description"],
                            location=bug["location"], severity=bug["severity"], category="planted", what_if=bug["what_if"],
                            fix=bug["fix"], confidence=round(gate.confidence, 3), low_confidence=gate.low_confidence, status="hidden")
            s.add(row)
            await s.flush()
            bug_ids.append(str(row.id))
        if bug_ids:
            secret["planted_bug_ids"] = bug_ids
        ch.secret = secret
        ch.meta = {**dict(ch.meta or {}), "question": as_str(d.get("question"), 1000),
                   "skills": [as_str(x, 60) for x in as_list(d.get("skills"), 6)],
                   "estimated_minutes": clamp(d.get("estimated_minutes"), 1, 240), "items": len(secret_items(ch)),
                   "generation": gate.meta(), "error": None}
        ch.status = "open"
        await s.commit()
    return JobOutcome(result={"challenge_id": str(challenge_id)}, confidence=gate.confidence, low_confidence=gate.low_confidence,
                      provider=gate.provider)


@on_failure("challenge_generate")
async def _generate_failed(ctx: JobContext, error: str) -> None:
    async with SessionLocal() as s:
        ch = await s.get(Challenge, uuid.UUID(ctx.payload["challenge_id"]), with_for_update=True)
        if ch is not None and ch.status == "generating":
            ch.status = "failed"
            ch.meta = {**dict(ch.meta or {}), "error": error[:400]}
            await s.commit()


@job_handler("challenge_evaluate")
async def challenge_evaluate(ctx: JobContext) -> JobOutcome:
    challenge_id = uuid.UUID(ctx.payload["challenge_id"])
    self_conf = clamp(ctx.payload.get("confidence"))
    async with SessionLocal() as s:
        ch = await s.get(Challenge, challenge_id)
        if ch is None:
            raise RuntimeError("challenge no longer exists")
        user = await load_user(s, ctx.user_id)
    level = get_level(ch.level)
    items = secret_items(ch)
    system = prompts.CHALLENGE_EVAL_SYSTEM + ("\n" + prompts.BUILD_EVAL_EXTRA if ch.kind == "build" else "")
    gate = await ask_json(
        "coder", system,
        prompts.challenge_eval_prompt({"kind": ch.kind, "level": ch.level, "title": ch.title, "prompt": ch.prompt, "code": ch.code,
                                       "secret": ch.secret}, items, ch.submission, int(self_conf) if self_conf is not None else None),
        user=user, required=("score", "feedback"), temperature=0.2, max_tokens=1300 + 100 * level.number,
        samples=level.samples, agreement_key="score",
    )
    d = gate.data
    score = clamp(d.get("score"), default=0.0) or 0.0
    found: list[int] = []
    for raw in as_list(d.get("found_items"), 50):
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(items) and idx not in found:
            found.append(idx)
    threshold = get_settings().ai.confidence_threshold
    evaluation: dict[str, Any] = {
        "score": round(score, 1), "feedback": as_str(d.get("feedback"), 4000),
        "hints": [as_str(h, 400) for h in as_list(d.get("hints"), 6)], "found_items": sorted(found),
        "total_items": len(items), "self_confidence": self_conf, "meta": gate.meta(),
    }
    async with SessionLocal() as s:
        user = await load_user(s, ctx.user_id, lock=True)
        ch = await s.get(Challenge, challenge_id, with_for_update=True, populate_existing=True)
        if ch is None:
            raise RuntimeError("challenge deleted")
        if ch.status != "evaluating":  # already evaluated by a parallel job, or released after an outage
            return JobOutcome(result={"skipped": f"challenge is '{ch.status}'"}, confidence=gate.confidence,
                              low_confidence=gate.low_confidence, provider=gate.provider)
        secret = dict(ch.secret or {})
        if ch.kind == "bug_hunt":
            ids = secret.get("planted_bug_ids") or []
            for idx in found:
                if idx < len(ids):
                    bug = await s.get(HiddenBug, uuid.UUID(ids[idx]))
                    if bug is not None and bug.status == "hidden":
                        bug.status = "found"
                        bug.resolved_at = utcnow()
        if ch.kind == "build":
            new_ids = []
            for item in as_list(d.get("bugs_in_submission"), 6):
                bug = _bug(item)
                if bug:
                    row = HiddenBug(user_id=ch.user_id, challenge_id=ch.id, title=bug["title"], description=bug["description"],
                                    location=bug["location"], severity=bug["severity"], category="submission",
                                    what_if=bug["what_if"], fix=bug["fix"], confidence=round(gate.confidence, 3),
                                    low_confidence=gate.confidence < threshold, status="hidden")
                    s.add(row)
                    await s.flush()
                    new_ids.append(str(row.id))
            secret["submission_bug_ids"] = new_ids
            heur = heuristic_ai_likelihood(ch.submission)
            evaluation["ai_detection"] = {
                "likelihood": combine(heur["likelihood"], clamp(d.get("ai_likelihood"), 0.0, 1.0), gate.confidence),
                "signals": heur["ai_signals"], "human_signals": heur["human_signals"],
                "note": "AI assistance is fine - ownership of the idea is what we check next.",
            }
            evaluation["ownership_questions"] = [as_str(q, 300) for q in as_list(d.get("ownership_questions"), 4)]
            evaluation["hidden_bugs_in_submission"] = len(new_ids)
        ch.secret = secret
        ch.score = round(score, 1)
        ch.status = "evaluated"
        ch.evaluated_at = utcnow()
        ch.evaluation = evaluation
        weight = gate.confidence * (0.6 if gate.low_confidence else 1.0)
        add_observations(s, user.id, d.get("dimension_scores"), confidence=weight, source="challenge", cap=0.7,
                         evidence_prefix=f"[{ch.kind.replace('_', ' ').capitalize()} challenge: {ch.title}] ")
        calibration = calibration_score(self_conf, score)
        if calibration is not None:
            s.add(Observation(user_id=user.id, dimension="metacognition", score=round(calibration, 1), confidence=0.5,
                              source="calibration", evidence=f"self-confidence {self_conf:.0f}% vs score {score:.0f}"))
        extra = ""
        if ch.kind == "build" and evaluation.get("hidden_bugs_in_submission"):
            extra = f"\n\n🔒 I also found **{evaluation['hidden_bugs_in_submission']} hidden bug(s) in your code** - hunt them in the challenge's next step!"
        s.add(Message(user_id=user.id, role="assistant", content=(
            f"📝 **Challenge evaluated - _{ch.title}_:** {score:.0f}/100"
            + (f" ({len(found)}/{len(items)} key items)" if items else "") + f"\n\n{evaluation['feedback'][:1500]}{extra}"
        ), meta={"turn_type": "challenge_result", "mood": "proud" if score >= 70 else "thinking", "level": ch.level,
                 "challenge_id": str(ch.id), **gate.meta()}))
        await s.flush()
        await apply_progress(s, user, round(level.progress_gain * (0.6 + 1.2 * score / 100.0), 2))
        await remember(s, user.id, "challenge", f"Challenge '{ch.title}' ({ch.kind}, level {ch.level}): student scored {score:.0f}/100.",
                       source_ref=f"challenge:{ch.id}", importance=0.55)
        await s.commit()
    return JobOutcome(result={"score": score, "found": len(found), "items": len(items)}, confidence=gate.confidence,
                      low_confidence=gate.low_confidence, provider=gate.provider)


@on_failure("challenge_evaluate")
async def _evaluate_failed(ctx: JobContext, error: str) -> None:
    async with SessionLocal() as s:
        ch = await s.get(Challenge, uuid.UUID(ctx.payload["challenge_id"]), with_for_update=True)
        if ch is not None and ch.status == "evaluating":
            ch.status = "open"
            ch.meta = {**dict(ch.meta or {}), "error": error[:400]}
            await s.commit()
