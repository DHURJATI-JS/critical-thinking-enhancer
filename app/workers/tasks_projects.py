"""Project jobs: ingest (OCR + pgvector chunks) -> 4 parallel AI analyses -> aggregate; bug-hunt guesses."""
from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from typing import Any

from sqlalchemy import select

from ..ai import prompts
from ..ai.embeddings import embed_texts
from ..ai.gate import ask_json
from ..ai.router import role_budget
from ..config import UPLOAD_DIR, get_settings
from ..db import SessionLocal
from ..models import HiddenBug, Job, Message, Observation, Project, ProjectChunk, json_merge, json_set, utcnow
from ..ocr.pipeline import run_ocr
from ..services.ai_detect import combine, heuristic_ai_likelihood
from ..services.ingest import build_content, chunk_text, excerpt
from ..services.levels import Level, get_level
from ..services.profile import calibration_score
from ..services.progression import normalize
from .common import add_observations, apply_progress, as_list, as_str, clamp, load_user
from .registry import JobContext, JobOutcome, job_handler, on_failure

MAX_CHUNKS = 400
SEVERITIES = ("low", "medium", "high", "critical")
_FILE_SPLIT = re.compile(r"^### FILE: (.+)$", re.MULTILINE)


def _excerpt_chars(level: Level, role: str) -> int:
    """Code excerpt size: grows with the level, never overflows the model window (reply + prompt reserved)."""
    tokens = min(role_budget(role) - 3000, 3000 + level.number * 1200)
    return max(3000, int(tokens * 3.0))


def _split_files(content: str) -> list[tuple[str, str]]:
    parts = _FILE_SPLIT.split(content)
    if len(parts) < 3:
        return [("project", content)] if content.strip() else []
    return [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


async def _load(ctx: JobContext) -> tuple[Project, Any, Level]:
    async with SessionLocal() as s:
        project = await s.get(Project, uuid.UUID(ctx.payload["project_id"]))
        if project is None:
            raise RuntimeError("project no longer exists")
        user = await load_user(s, ctx.user_id)
    return project, user, get_level(normalize(user.progression)["level"])


# --------------------------------------------------------------------------- ingest + fan-out
@job_handler("project_ingest")
async def project_ingest(ctx: JobContext) -> JobOutcome:
    from .queue import queue

    project, user, _ = await _load(ctx)
    folder = UPLOAD_DIR / str(project.id)
    image_names = {f.get("stored"): f.get("path") for f in (project.files or []) if isinstance(f, dict) and f.get("kind") == "image"}
    images = sorted(p for p in folder.glob("*") if p.is_file()) if folder.exists() else []
    ocr_entries: list[dict[str, Any]] = []
    ocr_texts: list[tuple[str, str]] = []
    if images:
        payloads = [await asyncio.to_thread(p.read_bytes) for p in images]
        results = await asyncio.gather(*(run_ocr(data, user=user) for data in payloads), return_exceptions=True)
        for path, res in zip(images, results):
            name = image_names.get(path.name, path.name)
            if isinstance(res, BaseException):
                ocr_entries.append({"file": name, "error": f"{res.__class__.__name__}: {res}"[:200]})
                continue
            ocr_entries.append({"file": name, "engine": res["engine"], "confidence": res["confidence"],
                                "low_confidence": res["low_confidence"], "chars": len(res["text"]), "layers": res["layers"]})
            if res["text"].strip():
                ocr_texts.append((f"{name} [OCR via {res['engine']}, confidence {res['confidence']:.0%}]", res["text"]))
        # the screenshots are deleted only AFTER their OCR text is safely committed (see below) - if this job dies
        # first (outage, timeout), "Analyse" can still read them again

    content = project.content or ""
    if ocr_texts:
        content = (content + "\n" + build_content(ocr_texts)).strip()
    pieces: list[tuple[str, int, str]] = []
    for path, body in _split_files(content):
        for i, chunk in enumerate(chunk_text(body)):
            pieces.append((path, i, chunk))
    pieces = pieces[:MAX_CHUNKS]
    vectors: list[tuple[str, list[float]]] = []
    for start in range(0, len(pieces), 32):
        batch = pieces[start : start + 32]
        name, vecs = await embed_texts([f"{p}\n{c}" for p, _, c in batch])
        vectors.extend((name, v) for v in vecs)

    async with SessionLocal() as s:
        project = await s.get(Project, project.id, with_for_update=True)
        if project is None:
            raise RuntimeError("project deleted during ingest")
        project.content = content
        json_set(project.analysis, "ocr", ocr_entries)
        json_set(project.analysis, "chunks", len(pieces))
        if not content.strip():
            project.status = "failed"
            json_set(project.analysis, "error", "No readable code or text was found in the upload.")
            await s.commit()
            if images:
                shutil.rmtree(folder, ignore_errors=True)
            return JobOutcome(result={"error": "empty project"})
        project.status = "analyzing"
        for (path, idx, chunk), (embed_model, vec) in zip(pieces, vectors):
            s.add(ProjectChunk(project_id=project.id, user_id=project.user_id, path=path[:400], idx=idx, content=chunk,
                               embed_model=embed_model, embedding=vec))
        payload = {"project_id": str(project.id)}
        group = await queue.fan_out(
            s,
            group_kind="project_group",
            user_id=project.user_id,
            label=f"Analyse '{project.name}' - 4 parallel AI jobs",
            children=[
                {"kind": "project_review", "payload": payload, "label": "Qwen coder: code review & level"},
                {"kind": "project_bughunt", "payload": payload, "label": "Qwen coder: secret bug hunt"},
                {"kind": "project_ai_detect", "payload": payload, "label": "AI-assistance detection"},
                {"kind": "project_cognitive", "payload": payload, "label": "Gateway: thinking-skills read"},
            ],
            on_complete={"kind": "project_aggregate", "payload": payload, "label": f"Aggregate analysis of '{project.name}'"},
            priority=1,
        )
        json_set(project.analysis, "group_job_id", group.id)
        await s.commit()
    if images:
        shutil.rmtree(folder, ignore_errors=True)
    await queue.notify()
    return JobOutcome(result={"chunks": len(pieces), "ocr_images": len(ocr_entries), "group_job_id": group.id})


@on_failure("project_ingest")
@on_failure("project_aggregate")
async def _project_failed(ctx: JobContext, error: str) -> None:
    async with SessionLocal() as s:
        project = await s.get(Project, uuid.UUID(ctx.payload["project_id"]), with_for_update=True)
        if project is not None and project.status != "ready":
            project.status = "failed"
            json_set(project.analysis, "error", error[:400])
            await s.commit()


# --------------------------------------------------------------------------- parallel analyses
@job_handler("project_review")
async def project_review(ctx: JobContext) -> JobOutcome:
    project, user, level = await _load(ctx)
    code = excerpt(project.content, _excerpt_chars(level, "coder"))
    gate = await ask_json(
        "coder", prompts.CODE_REVIEW_SYSTEM, prompts.code_prompt(project.name, project.description, code), user=user,
        required=("summary", "level_estimate"), temperature=0.2, max_tokens=1100,
        samples=1 if level.number < 6 else 2, agreement_key="level_estimate", need_tokens=int(len(code) / 3.6),
    )
    d = gate.data
    review = {
        "summary": as_str(d.get("summary"), 1200),
        "languages": [as_str(x, 30) for x in as_list(d.get("languages"), 8)],
        "level_estimate": clamp(d.get("level_estimate"), 1, 10),
        "level_reason": as_str(d.get("level_reason"), 600),
        "strengths": [as_str(x, 300) for x in as_list(d.get("strengths"), 6)],
        "improvements": [as_str(x, 300) for x in as_list(d.get("improvements"), 6)],
        "complexity": as_str(d.get("complexity"), 20),
        "design_decisions": [as_str(x, 300) for x in as_list(d.get("design_decisions"), 5)],
        "meta": gate.meta(),
    }
    async with SessionLocal() as s:
        p = await s.get(Project, project.id, with_for_update=True)
        if p is not None:
            json_set(p.analysis, "review", review)
            await s.commit()
    return JobOutcome(result={"level_estimate": review["level_estimate"]}, confidence=gate.confidence,
                      low_confidence=gate.low_confidence, provider=gate.provider)


@job_handler("project_bughunt")
async def project_bughunt(ctx: JobContext) -> JobOutcome:
    project, user, level = await _load(ctx)
    max_bugs = 3 + level.number // 2
    code = excerpt(project.content, _excerpt_chars(level, "coder"))
    gate = await ask_json(
        "coder", prompts.BUG_HUNT_SYSTEM,
        prompts.code_prompt(project.name, project.description, code, extra=f"Report at most {max_bugs} real bugs."),
        user=user, required=("bugs",), temperature=0.2, max_tokens=1700, need_tokens=int(len(code) / 3.6),
    )
    threshold = get_settings().ai.confidence_threshold
    severities: dict[str, int] = {}
    count = 0
    async with SessionLocal() as s:
        for bug in as_list(gate.data.get("bugs"), max_bugs):
            if not isinstance(bug, dict) or not str(bug.get("title") or "").strip():
                continue
            severity = str(bug.get("severity") or "medium").lower()
            severity = severity if severity in SEVERITIES else "medium"
            conf = clamp(bug.get("confidence"), 0.0, 1.0, gate.confidence) or 0.0
            s.add(HiddenBug(
                user_id=project.user_id, project_id=project.id, title=as_str(bug.get("title"), 240),
                description=as_str(bug.get("description"), 2000), location=as_str(bug.get("location"), 240),
                severity=severity, category=as_str(bug.get("category"), 40) or "logic",
                what_if=as_str(bug.get("what_if"), 600), fix=as_str(bug.get("fix"), 2000),
                confidence=round(conf, 3), low_confidence=conf < threshold or gate.low_confidence, status="hidden",
            ))
            severities[severity] = severities.get(severity, 0) + 1
            count += 1
        p = await s.get(Project, project.id, with_for_update=True)
        if p is not None:
            json_set(p.analysis, "bug_summary", {"count": count, "by_severity": severities, "meta": gate.meta()})
        await s.commit()
    return JobOutcome(result={"hidden_bugs": count}, confidence=gate.confidence, low_confidence=gate.low_confidence, provider=gate.provider)


@job_handler("project_ai_detect")
async def project_ai_detect(ctx: JobContext) -> JobOutcome:
    project, user, level = await _load(ctx)
    heur = heuristic_ai_likelihood(project.content[:80000])
    code = excerpt(project.content, min(_excerpt_chars(level, "coder"), 24000))
    gate = await ask_json(
        "coder", prompts.AI_DETECT_SYSTEM,
        prompts.code_prompt(project.name, project.description, code,
                            extra=f"Heuristic AI signals: {heur['ai_signals']}; human signals: {heur['human_signals']}"),
        user=user, required=("ai_likelihood",), temperature=0.2, max_tokens=700, need_tokens=int(len(code) / 3.6),
    )
    model_value = clamp(gate.data.get("ai_likelihood"), 0.0, 1.0)
    detection = {
        "likelihood": combine(heur["likelihood"], model_value, gate.confidence),
        "heuristic": heur["likelihood"],
        "model": model_value,
        "ai_signals": (heur["ai_signals"] + [as_str(x, 200) for x in as_list(gate.data.get("ai_signals"), 6)])[:10],
        "human_signals": (heur["human_signals"] + [as_str(x, 200) for x in as_list(gate.data.get("human_signals"), 6)])[:10],
        "parts_to_explain": [as_str(x, 300) for x in as_list(gate.data.get("parts_to_explain"), 5)],
        "note": "AI assistance is fine in the AI era - what matters is that the idea and the decisions are yours.",
        "meta": gate.meta(),
    }
    async with SessionLocal() as s:
        p = await s.get(Project, project.id, with_for_update=True)
        if p is not None:
            json_set(p.analysis, "ai_detection", detection)
            await s.commit()
    return JobOutcome(result={"likelihood": detection["likelihood"]}, confidence=gate.confidence,
                      low_confidence=gate.low_confidence, provider=gate.provider)


@job_handler("project_cognitive")
async def project_cognitive(ctx: JobContext) -> JobOutcome:
    project, user, level = await _load(ctx)
    code = excerpt(project.content, min(_excerpt_chars(level, "gateway"), 30000))
    gate = await ask_json(
        "gateway", prompts.PROJECT_COGNITIVE_SYSTEM, prompts.code_prompt(project.name, project.description, code),
        user=user, required=("observations",), temperature=0.3, max_tokens=900, need_tokens=int(len(code) / 3.6),
    )
    scores: dict[str, Any] = {}
    for obs in as_list(gate.data.get("observations"), 8):
        dim = obs.get("dimension") if isinstance(obs, dict) else None
        if isinstance(dim, str) and dim and dim not in scores:  # a list / object here must not crash the job
            scores[dim] = {"score": obs.get("score"), "evidence": obs.get("evidence")}
    async with SessionLocal() as s:
        added = add_observations(s, project.user_id, scores, confidence=gate.confidence * 0.7, source="project", cap=0.45,
                                 evidence_prefix=f"[{project.name}] ")
        p = await s.get(Project, project.id, with_for_update=True)
        if p is not None:
            json_set(p.analysis, "cognitive", {"questions_to_ask": [as_str(q, 300) for q in as_list(gate.data.get("questions_to_ask"), 5)],
                                               "observations_added": added, "meta": gate.meta()})
        await s.commit()
    return JobOutcome(result={"observations": added}, confidence=gate.confidence, low_confidence=gate.low_confidence, provider=gate.provider)


@job_handler("project_aggregate")
async def project_aggregate(ctx: JobContext) -> JobOutcome:
    group_id = int(ctx.payload["group_id"])
    async with SessionLocal() as s:
        children = list((await s.execute(select(Job.kind, Job.status, Job.low_confidence, Job.confidence).where(Job.parent_id == group_id))).all())
        # lock order everywhere: user row first, then project (the reverse order could deadlock with a bug hunt)
        user = await load_user(s, ctx.user_id, lock=True)
        project = await s.get(Project, uuid.UUID(ctx.payload["project_id"]), with_for_update=True, populate_existing=True)
        if project is None or project.user_id != user.id:
            raise RuntimeError("project no longer exists")
        succeeded = [c for c in children if c.status == "succeeded"]
        project.status = "ready" if succeeded else "failed"
        if succeeded:  # a new analysis worked: an older "interrupted" / error note is no longer true
            project.analysis = {k: v for k, v in dict(project.analysis or {}).items() if k != "error"}
        a = dict(project.analysis or {})
        review = a.get("review") or {}
        det = a.get("ai_detection") or {}
        bugs = a.get("bug_summary") or {}
        confs = [c.confidence for c in succeeded if c.confidence is not None]
        low = any(c.low_confidence for c in succeeded)
        lines = [f"🔍 **I've analysed _{project.name}_** ({len(succeeded)}/{len(children)} parallel analyses succeeded)."]
        if review.get("level_estimate"):
            lines.append(f"- **Estimated level:** {review['level_estimate']:.0f}/10 - {review.get('level_reason') or review.get('summary', '')}")
        if det.get("likelihood") is not None:
            lines.append(f"- **AI-assistance estimate:** {det['likelihood'] * 100:.0f}% - that's okay! What matters is that the idea and decisions are yours.")
        if bugs.get("count"):
            sev = ", ".join(f"{n} {k}" for k, n in sorted(bugs.get("by_severity", {}).items()))
            lines.append(f"- 🔒 **I found {bugs['count']} hidden bug(s)** ({sev}). I'm keeping them secret for now - can you find them? Open **Projects -> Bug Hunt**.")
        elif "bug_summary" in a:
            lines.append("- I didn't find clear bugs - but let's stress-test it with some what-ifs anyway.")
        decisions = as_list(review.get("design_decisions"), 1)
        if decisions:
            lines.append(f"\nLet's start here: **why** did you go with this? _{decisions[0]}_")
        content = "\n".join(lines)
        if project.status == "failed":
            content = f"⚠️ I couldn't analyse _{project.name}_ - all analysis jobs failed. Please try again once the AI servers are healthy."
        s.add(Message(user_id=user.id, role="assistant", content=content, meta={
            "turn_type": "project_report", "mood": "thinking", "level": normalize(user.progression)["level"],
            "project_id": str(project.id), "confidence": round(min(confs), 3) if confs else 0.0,
            "low_confidence": low or not confs, "threshold": get_settings().ai.confidence_threshold,
            "question": f"Why did you choose: {decisions[0]}" if decisions else None,
        }))
        if review.get("summary"):
            from ..services.memory import remember

            await remember(s, user.id, "project", f"Project '{project.name}': {review['summary']} (estimated level {review.get('level_estimate')}/10)",
                           source_ref=f"project:{project.id}", importance=0.7)
            json_merge(user.cognitive, "project_levels", {str(project.id): review.get("level_estimate")})
        await s.commit()
    return JobOutcome(result={"status": project.status, "succeeded": len(succeeded), "children": len(children)},
                      confidence=min(confs) if confs else None, low_confidence=low)


# --------------------------------------------------------------------------- bug hunt (guesses)
@job_handler("bug_guess_eval")
async def bug_guess_eval(ctx: JobContext) -> JobOutcome:
    guesses = [as_str(g, 800) for g in as_list(ctx.payload.get("guesses"), 10) if str(g).strip()]
    self_conf = clamp(ctx.payload.get("confidence"))
    project_id = ctx.payload.get("project_id")
    challenge_id = ctx.payload.get("challenge_id")
    async with SessionLocal() as s:
        user = await load_user(s, ctx.user_id)
        stmt = select(HiddenBug).where(HiddenBug.user_id == user.id, HiddenBug.status == "hidden")
        if project_id:
            stmt = stmt.where(HiddenBug.project_id == uuid.UUID(project_id))
            project = await s.get(Project, uuid.UUID(project_id))
            code = project.content if project else ""
            title = project.name if project else "project"
        else:
            from ..models import Challenge

            stmt = stmt.where(HiddenBug.challenge_id == uuid.UUID(challenge_id))
            challenge = await s.get(Challenge, uuid.UUID(challenge_id))
            code = (challenge.submission or challenge.code) if challenge else ""
            title = challenge.title if challenge else "challenge"
        bugs = list((await s.execute(stmt)).scalars())
    level = get_level(normalize(user.progression)["level"])
    if not bugs:
        return JobOutcome(result={"found": [], "remaining": 0, "feedback": "There are no hidden bugs left here - nice work!"})
    gate = await ask_json(
        "coder", prompts.BUG_GUESS_SYSTEM,
        prompts.bug_guess_prompt([{"title": b.title, "location": b.location, "description": b.description} for b in bugs],
                                 guesses, excerpt(code, 12000)),
        user=user, required=("matches",), temperature=0.1, max_tokens=900, samples=level.samples, agreement_key="foresight_score",
    )
    matched: set[int] = set()
    for m in as_list(gate.data.get("matches"), 20):
        if not isinstance(m, dict):
            continue
        try:
            idx = int(m.get("bug_index"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(bugs) and (clamp(m.get("quality"), 0.0, 1.0, 0.6) or 0) >= 0.5:
            matched.add(idx)
    matched_bugs = [bugs[i] for i in sorted(matched)]
    async with SessionLocal() as s:
        user = await load_user(s, ctx.user_id, lock=True)  # lock order everywhere: user row first, then project
        # Only bugs that are STILL hidden count: a parallel hunt (two tabs, a retry) or a reveal in the meantime
        # must not be scored - or rewarded - twice.
        found = []
        for bug in matched_bugs:
            row = await s.get(HiddenBug, bug.id, with_for_update=True, populate_existing=True)
            if row is not None and row.status == "hidden":
                row.status = "found"
                row.resolved_at = utcnow()
                found.append(bug)
        foresight = clamp(gate.data.get("foresight_score"), default=100.0 * len(found) / len(bugs)) or 0.0
        conf = gate.confidence * (0.6 if gate.low_confidence else 1.0)
        s.add(Observation(user_id=user.id, dimension="foresight", score=round(foresight, 1), confidence=round(min(0.65, conf), 3),
                          source="bughunt", evidence=f"found {len(found)}/{len(bugs)} hidden bugs in {title}"))
        calibration = calibration_score(self_conf, 100.0 * len(found) / len(bugs))
        if calibration is not None:
            s.add(Observation(user_id=user.id, dimension="metacognition", score=round(calibration, 1), confidence=0.45,
                              source="calibration", evidence=f"self-confidence {self_conf:.0f}% vs {len(found)}/{len(bugs)} found"))
        level = get_level(normalize(user.progression)["level"])
        # progress follows the SHARE of bugs found; a hunt that finds nothing earns nothing (no farming)
        gain = round(level.progress_gain * (0.3 + 0.9 * len(found) / len(bugs)), 2) if found else 0.0
        remaining = len(bugs) - len(found)
        s.add(Message(user_id=user.id, role="assistant", content=(
            f"🐞 **Bug hunt on _{title}_:** you found **{len(found)} of {len(bugs)}** hidden bug(s)"
            + (f" - {remaining} still hidden. " if remaining else " - all of them! ")
            + as_str(gate.data.get("feedback"), 1500)
        ), meta={"turn_type": "bughunt_result", "mood": "proud" if found else "thinking", "level": level.number, **gate.meta()}))
        await s.flush()
        if gain > 0:
            await apply_progress(s, user, gain)
        if project_id:
            p = await s.get(Project, uuid.UUID(project_id), with_for_update=True)
            if p is not None:
                attempts = list((p.analysis or {}).get("bughunt_attempts") or [])
                attempts.append({"at": utcnow().isoformat(), "guesses": len(guesses), "found": len(found), "confidence": self_conf})
                json_set(p.analysis, "bughunt_attempts", attempts[-20:])
        await s.commit()
    return JobOutcome(
        result={
            "found": [{"id": str(b.id), "title": b.title, "location": b.location, "severity": b.severity,
                       "description": b.description, "fix": b.fix} for b in found],
            "remaining": len(bugs) - len(found),
            "feedback": as_str(gate.data.get("feedback"), 2000),
            "false_alarms": as_list(gate.data.get("false_alarms"), 10),
            "valid_new_findings": [as_str(x, 300) for x in as_list(gate.data.get("valid_new_findings"), 6)],
            "foresight_score": round(foresight, 1),
            "gain": gain,
        },
        confidence=gate.confidence,
        low_confidence=gate.low_confidence,
        provider=gate.provider,
    )
