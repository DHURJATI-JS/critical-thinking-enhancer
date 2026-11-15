"""Chat-side jobs: onboarding analysis, chat turns, profile/SWOT refresh, OCR of chat images."""
from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from ..ai import prompts
from ..ai.gate import GateResult, ask_json
from ..ai.router import fallback_ready, role_budget
from ..config import UPLOAD_DIR
from ..db import SessionLocal
from ..models import Challenge, HiddenBug, Memory, Message, Observation, Project, SpamEvent, json_set, utcnow
from ..ocr.pipeline import run_ocr
from ..services.levels import Level, get_level
from ..services.memory import pick_callback, recall, remember, search_chunks
from ..services.profile import summarize
from ..services.progression import normalize
from .common import (
    add_observations,
    apply_progress,
    as_list,
    as_str,
    clamp,
    load_user,
    maybe_queue_profile_refresh,
    observation_count,
    profile_for_ai,
    tutor_name,
)
from .registry import JobContext, JobOutcome, job_handler, on_failure

CHARS_PER_TOKEN = 3.0  # conservative: code and markdown tokenize worse than prose
SWOT_KEYS = ("strengths", "weaknesses", "opportunities", "threats")
SWOT_STATUSES = {"supported", "contradicted", "unverified", "needs_more_chat"}


def _trim(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 14)] + " ...[trimmed]"


def _ago(when: datetime | None) -> str:
    if when is None:
        return "earlier"
    minutes = int((datetime.now(timezone.utc) - when).total_seconds() // 60)
    if minutes < 60:
        return f"{max(1, minutes)} minutes ago"
    if minutes < 60 * 24:
        return f"{minutes // 60} hours ago"
    return f"{minutes // (60 * 24)} days ago"


def normalize_swot(raw: Any) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    raw = raw if isinstance(raw, dict) else {}
    for key in SWOT_KEYS:
        items = []
        for item in as_list(raw.get(key), 6):
            if isinstance(item, str):
                item = {"point": item}
            if not isinstance(item, dict) or not str(item.get("point") or "").strip():
                continue
            status = str(item.get("status") or "needs_more_chat").strip().lower()
            items.append({
                "point": as_str(item.get("point"), 300),
                "status": status if status in SWOT_STATUSES else "needs_more_chat",
                "note": as_str(item.get("note"), 300),
            })
        if not items:
            items = [{"point": "Not enough information yet", "status": "needs_more_chat",
                      "note": "More AI chatting is required before concluding anything here."}]
        out[key] = items
    return out


def redact_secrets(reply: str, secrets: list[str]) -> str:
    """Last line of defence: never leak a hidden bug's title even if the model tries."""
    for title in secrets:
        title = (title or "").strip()
        if len(title) >= 10 and title.lower() in reply.lower():
            reply = re.sub(re.escape(title), "🔒 [hidden - find it yourself]", reply, flags=re.IGNORECASE)
    return reply


def _default_opening(name: str, tutor: str) -> str:
    return (
        f"Hi {name}! I'm **{tutor}**, your critical-thinking coach. 👋\n\n"
        "Let's start with you: **tell me about a coding project you've built** - what does it do, why did you build it "
        "that way, and what was the hardest bug you hit?"
    )


# --------------------------------------------------------------------------- onboarding
@job_handler("onboarding_analysis")
async def onboarding_analysis(ctx: JobContext) -> JobOutcome:
    async with SessionLocal() as s:
        user = await load_user(s, ctx.user_id)
        project_names = list((await s.execute(select(Project.name).where(Project.user_id == user.id))).scalars())
    tutor = tutor_name(user)
    gate = await ask_json(
        "gateway",
        prompts.ONBOARD_SYSTEM,
        prompts.onboard_prompt(profile_for_ai(user), tutor, project_names),
        user=user,
        required=("opening_message", "swot_analysis"),
        temperature=0.5,
        max_tokens=1300,
    )
    data = gate.data
    name = (user.profile or {}).get("name") or user.display_name
    opening = as_str(data.get("opening_message"), 3000) or _default_opening(name, tutor)
    async with SessionLocal() as s:
        user = await load_user(s, ctx.user_id, lock=True)
        json_set(user.cognitive, "initial", {
            "summary": as_str(data.get("summary"), 800),
            "hypotheses": as_list(data.get("initial_hypotheses"), 8),
            "suggested_level": clamp(data.get("suggested_level"), 1, 10),
            "meta": gate.meta(),
            "at": utcnow().isoformat(),
        })
        json_set(user.cognitive, "swot_analysis", normalize_swot(data.get("swot_analysis")))
        json_set(user.cognitive, "swot_meta", gate.meta())
        level = normalize(user.progression)["level"]
        message = Message(user_id=user.id, role="assistant", content=opening,
                          meta={"turn_type": "opening", "mood": "happy", "level": level, **gate.meta()})
        s.add(message)
        profile = user.profile or {}
        if profile.get("bio"):
            await remember(s, user.id, "bio", f"Student biography: {profile['bio']}", importance=0.6)
        if profile.get("swot"):
            await remember(s, user.id, "swot", "Student's self-reported SWOT: " + json.dumps(profile["swot"], ensure_ascii=False), importance=0.5)
        await s.commit()
        return JobOutcome(result={"message_id": message.id}, confidence=gate.confidence, low_confidence=gate.low_confidence, provider=gate.provider)


# --------------------------------------------------------------------------- chat turn
def decide_plan(
    *,
    callback: Memory | None,
    undiscussed: Project | None,
    has_projects: bool,
    hints: list[str],
    open_challenge: Challenge | None,
    user_turns: int,
) -> tuple[str, str, dict[str, Any]]:
    if callback is not None:
        return (
            "recall",
            f"Briefly respond to the student's message. Then ask a MEMORY question about something from {_ago(callback.created_at)}: "
            f"\"{_trim(callback.content, 400)}\". Ask whether they remember it and to recall specifics (what, why, what-if) "
            "WITHOUT revealing those specifics yourself.",
            {"recall_memory": callback.content[:600], "recall_memory_id": callback.id},
        )
    if undiscussed is not None and (user_turns <= 2 or random.random() < 0.4):
        review = (undiscussed.analysis or {}).get("review") or {}
        decisions = as_list(review.get("design_decisions"), 3)
        decision = f" In particular ask WHY about this decision: {decisions[0]}." if decisions else ""
        return (
            "project",
            f"Ask about their project '{undiscussed.name}' ({_trim(str(review.get('summary') or undiscussed.description), 300)}). "
            f"Ask what it does and why they built it that way.{decision} Probe that the idea is theirs (AI-assisted code is fine).",
            {"project_id": str(undiscussed.id)},
        )
    if not has_projects and user_turns <= 3:
        return (
            "project",
            "The student has not shared a project yet. Ask about a coding project they built or want to build (what, why, "
            "hardest bug) and invite them to upload it in the Projects tab.",
            {},
        )
    if hints and random.random() < 0.3:
        return (
            "bug_hint",
            f"Respond, then steer toward this what-if about their own code (do NOT reveal the underlying bug): \"{random.choice(hints)}\"",
            {},
        )
    if user_turns % 6 == 5:
        return (
            "challenge_suggest",
            "Respond, then suggest a Qwen-coder challenge (Bug Hunt, Why?, Predict or Build) from the Challenges tab, aimed at a weaker area.",
            {},
        )
    if open_challenge is not None and random.random() < 0.2:
        return (
            "challenge_nudge",
            f"Respond, then remind them about their open challenge '{open_challenge.title}' with a what-if nudge (no spoilers).",
            {},
        )
    return (
        "socratic",
        "Respond to the student's message: acknowledge what is right, scrutinise what is weak according to the level rules, "
        "then ask ONE deeper what-if or why question.",
        {},
    )


def _cognitive_snapshot(user_cognitive: dict[str, Any], summary: dict[str, Any]) -> str:
    lines = []
    for dim in summary["dimensions"].values():
        state = "INSUFFICIENT EVIDENCE - do not conclude" if dim["status"] == "insufficient" else f"{dim['score']} ±{dim['margin']} ({dim['status']})"
        lines.append(f"- {dim['label']}: {state}")
    swot = user_cognitive.get("swot_analysis") or {}
    for key in SWOT_KEYS:
        points = "; ".join(f"{p.get('point')} [{p.get('status')}]" for p in as_list(swot.get(key), 4) if isinstance(p, dict))
        if points:
            lines.append(f"- AI SWOT {key}: {points}")
    return "\n".join(lines)


@job_handler("chat_turn")
async def chat_turn(ctx: JobContext) -> JobOutcome:
    message_id = int(ctx.payload["message_id"])
    async with SessionLocal() as s:
        user = await load_user(s, ctx.user_id)
        user_msg = await s.get(Message, message_id)
        if user_msg is None or user_msg.user_id != user.id:
            raise RuntimeError("message not found")
        prog = normalize(user.progression)
        level: Level = get_level(prog["level"])
        long_ctx = bool(((user.settings or {}).get("fallback") or {}).get("for_long_context")) and fallback_ready(user)
        # Reserve room for the reply (up to 1.6x on confidence retries), the system prompt and the student's
        # message, so the prompt never overflows the model's window (e.g. 8,192 tokens on llama.cpp).
        reserve = int(level.max_tokens * 1.6) + 900 + len(user_msg.content) // 3
        budget = role_budget("gateway")
        if long_ctx and level.context_tokens + reserve > budget:
            eff_tokens = level.context_tokens  # too big for this device: the router sends it to the user's own key
        else:  # the local gateway serves it: memory + reply must fit its window
            eff_tokens = max(1024, min(level.context_tokens, budget - reserve))
        need_tokens = eff_tokens + reserve  # what the whole request needs; the router compares it to the window
        total_chars = int(eff_tokens * CHARS_PER_TOKEN)

        previous = list(reversed((await s.execute(
            select(Message)
            .where(Message.user_id == user.id, Message.id < user_msg.id, Message.role.in_(("user", "assistant")))
            .order_by(Message.id.desc())
            .limit(level.history)
        )).scalars().all()))
        last_assistant = next((m for m in reversed(previous) if m.role == "assistant" and not (m.meta or {}).get("rule_based")), None)
        last_meta = (last_assistant.meta or {}) if last_assistant else {}
        recall_target = last_meta.get("recall_memory") if last_meta.get("turn_type") == "recall" else None
        last_question = last_meta.get("question") or (last_assistant.content[-500:] if last_assistant else None)
        turns_since_recall = 0
        for m in reversed(previous):
            if m.role == "assistant":
                if (m.meta or {}).get("turn_type") == "recall":
                    break
                turns_since_recall += 1

        memories = await recall(s, user.id, user_msg.content, level.recalls)
        chunks = await search_chunks(s, user.id, user_msg.content, 1 + level.number // 3)
        projects = list((await s.execute(select(Project).where(Project.user_id == user.id).order_by(Project.created_at.desc()).limit(6))).scalars())
        hints = [h for h in (await s.execute(
            select(HiddenBug.what_if).where(HiddenBug.user_id == user.id, HiddenBug.status == "hidden", HiddenBug.project_id.is_not(None)).limit(6)
        )).scalars() if h]
        secret_titles = list((await s.execute(select(HiddenBug.title).where(HiddenBug.user_id == user.id, HiddenBug.status == "hidden"))).scalars())
        open_challenge = (await s.execute(
            select(Challenge).where(Challenge.user_id == user.id, Challenge.status == "open").order_by(Challenge.created_at.desc()).limit(1)
        )).scalars().first()
        summary = summarize((await s.execute(select(Observation).where(Observation.user_id == user.id))).scalars().all())
        user_turns = int((await s.execute(select(func.count()).select_from(Message).where(Message.user_id == user.id, Message.role == "user"))).scalar_one())
        callback = None
        if recall_target is None and turns_since_recall >= 4 and random.random() < level.recall_probability:
            callback = await pick_callback(s, user.id)

    undiscussed = next((p for p in projects if p.status == "ready" and not (p.analysis or {}).get("discussed")), None)
    plan_kind, plan_text, plan_meta = decide_plan(
        callback=callback, undiscussed=undiscussed, has_projects=bool(projects), hints=hints,
        open_challenge=open_challenge, user_turns=user_turns,
    )

    project_lines = []
    for p in projects:
        review = (p.analysis or {}).get("review") or {}
        project_lines.append(f"- {p.name} [{p.status}] languages={','.join(p.languages or []) or '?'} "
                             f"level_estimate={review.get('level_estimate', '?')}/10: {_trim(str(review.get('summary') or p.description), 220)}")
    context = {
        "SESSION": f"Level {level.number}/10 ({level.name}); level progress {prog['progress']:.0f}%; context memory {eff_tokens:,} tokens; student turn #{user_turns}",
        "STUDENT PROFILE (self-reported - use as reference)": _trim(json.dumps(profile_for_ai(user), ensure_ascii=False), int(total_chars * 0.16)),
        "COGNITIVE SNAPSHOT (do not jump to conclusions)": _trim(_cognitive_snapshot(user.cognitive or {}, summary), int(total_chars * 0.10)),
        "PROJECTS": _trim("\n".join(project_lines), int(total_chars * 0.08)),
        "LONG-TERM MEMORIES (semantic recall via pgvector)": _trim(
            "\n".join(f"- ({_ago(m.created_at)}) {_trim(m.content, 400)}" for m in memories), int(total_chars * 0.16)),
        "RELEVANT CODE FROM THEIR PROJECTS (pgvector)": _trim(
            "\n\n".join(f"[{c.path}]\n{c.content}" for c in chunks), int(total_chars * 0.18)),
        "HIDDEN BUG HINTS - never reveal the bug itself": _trim("\n".join(f"- {h}" for h in hints), int(total_chars * 0.05)),
        "OPEN CHALLENGE": _trim(f"{open_challenge.title}: {open_challenge.prompt}", int(total_chars * 0.05)) if open_challenge else "",
    }
    history_budget = int(total_chars * 0.18)
    history: list[dict[str, Any]] = []
    used = 0
    for m in reversed(previous):
        content = _trim(m.content, 1500)
        if used + len(content) > history_budget:
            break
        history.insert(0, {"role": m.role, "content": content})
        used += len(content)

    # Coding questions + code verification come ONLY from the Qwen coder. The gateway handles the human side
    # (feedback on reasoning, metacognition) - except memory checks, which are not coding questions.
    qwen_asks = plan_kind != "recall"
    tutor_plan = plan_text if not qwen_asks else (
        "React to the student's latest message as your rules describe. The Qwen code reviewer asks this turn's coding question.")
    tutor_job = ask_json(
        "gateway",
        prompts.tutor_system(level, tutor_name(user), qwen_asks=qwen_asks),
        prompts.tutor_prompt(context, tutor_plan, user_msg.content),
        user=user,
        history=history,
        required=("reply",),
        temperature=level.temperature,
        max_tokens=level.max_tokens,
        need_tokens=need_tokens,
        reasoning_effort=level.reasoning_effort if level.number >= 6 else None,
    )
    eval_job = ask_json(
        "gateway",
        prompts.EVALUATOR_SYSTEM,
        prompts.evaluator_prompt(last_question, user_msg.content, recall_target),
        user=user,
        temperature=0.2,
        max_tokens=600,
        samples=level.samples,
        agreement_key="answer_quality",
    )
    jobs = [tutor_job, eval_job]
    coach_index = spam_index = None
    if qwen_asks:
        coder_chars = max(2000, int((role_budget("coder") - 1500) * CHARS_PER_TOKEN * 0.55))
        recent = "\n".join(f"{m.role}: {_trim(m.content, 300)}" for m in previous[-4:])
        coach_context = {
            "TURN FOCUS (what to ask about)": plan_text,
            "PROJECTS": _trim("\n".join(project_lines), int(coder_chars * 0.15)),
            "RELEVANT CODE FROM THEIR PROJECTS (pgvector)": _trim("\n\n".join(f"[{c.path}]\n{c.content}" for c in chunks), int(coder_chars * 0.5)),
            "HIDDEN BUG HINTS - never reveal the bug itself": _trim("\n".join(f"- {h}" for h in hints), int(coder_chars * 0.08)),
            "OPEN CHALLENGE": _trim(f"{open_challenge.title}: {open_challenge.prompt}", int(coder_chars * 0.08)) if open_challenge else "",
            "RECENT CONVERSATION": _trim(recent, int(coder_chars * 0.15)),
        }
        last_code_q = next((((m.meta or {}).get("question")) for m in reversed(previous)
                            if m.role == "assistant" and (m.meta or {}).get("question_source") == "qwen-coder"), None)
        jobs.append(ask_json("coder", prompts.code_coach_system(level), prompts.code_coach_prompt(coach_context, user_msg.content, last_code_q),
                             user=user, required=("question",), temperature=max(0.2, level.temperature - 0.2), max_tokens=650,
                             need_tokens=int(coder_chars / CHARS_PER_TOKEN)))
        coach_index = len(jobs) - 1
    if ctx.payload.get("spam_check"):
        jobs.append(ask_json("gateway", prompts.SPAM_SYSTEM, f"MESSAGE:\n{user_msg.content}", user=user, temperature=0.0, max_tokens=200, max_retries=1))
        spam_index = len(jobs) - 1
    results = await asyncio.gather(*jobs, return_exceptions=True)
    tutor = results[0]
    if isinstance(tutor, BaseException):
        raise tutor
    assert isinstance(tutor, GateResult)
    evaluation = results[1] if isinstance(results[1], GateResult) else None
    coach = None
    if coach_index is not None:
        coach = results[coach_index]
        if isinstance(coach, BaseException):  # the Qwen coder is required: no coding question without it
            raise coach
    spam = results[spam_index] if spam_index is not None and isinstance(results[spam_index], GateResult) else None
    is_spam = bool(spam and spam.data.get("spam") is True and not spam.low_confidence)

    reply = as_str(tutor.data.get("reply"), 6000).strip()
    if not reply:
        reply = tutor.raw.strip()[:3000] if tutor.raw and not tutor.raw.lstrip().startswith("{") else "Let's look at this from another angle."
    mood = str(tutor.data.get("mood") or "neutral")
    question = as_str(tutor.data.get("question"), 600) or None
    code_check: dict[str, Any] = {}
    gate_meta = tutor.meta()
    if coach is not None:
        cd = coach.data
        question = as_str(cd.get("question"), 600) or None
        verdict = str(cd.get("verdict") or "none").lower()
        issues = [i for i in as_list(cd.get("issues"), 5) if isinstance(i, dict) and i.get("title")]
        parts = [reply]
        if verdict == "has_issues" and issues:
            parts.append("**Code check (Qwen coder):**\n" + "\n".join(
                f"- **{as_str(i.get('title'), 120)}** ({as_str(i.get('severity'), 10) or 'medium'}) - {as_str(i.get('detail'), 300)}" for i in issues))
        elif verdict == "correct":
            parts.append("**Code check (Qwen coder):** this looks correct - now let's stress-test it.")
        elif verdict == "unclear":
            parts.append("**Code check (Qwen coder):** I can't verify this yet - the code or claim is incomplete.")
        if question:
            parts.append(f"**{question}**")
        reply = "\n\n".join(p for p in parts if p)
        code_check = {"verdict": verdict, "issues": len(issues), "question_type": as_str(cd.get("question_type"), 20), "focus": as_str(cd.get("focus"), 120)}
        gate_meta = {**gate_meta, "confidence": round(min(tutor.confidence, coach.confidence), 3),
                     "low_confidence": tutor.low_confidence or coach.low_confidence,
                     "attempts": max(tutor.attempts, coach.attempts), "provider": f"{tutor.provider}+{coach.provider}",
                     "model": f"{tutor.model} + {coach.model}"}
    reply = redact_secrets(reply, secret_titles)

    async with SessionLocal() as s:
        user = await load_user(s, ctx.user_id, lock=True)
        level = get_level(normalize(user.progression)["level"])
        before = await observation_count(s, user.id)
        gain = 0.0
        genuine = False
        quality = 0.0
        eval_meta: dict[str, Any] = {}
        if is_spam:
            assert spam is not None
            s.add(SpamEvent(user_id=user.id, kind="chat_ai", score=spam.confidence, reason=as_str(spam.data.get("reason"), 300), blocked=True))
            reply = "🛡️ That message looks like spam or an attempt to extract hidden answers, so I won't process it. Let's get back to training!"
            mood, question = "concerned", None
        elif evaluation is not None:
            genuine = evaluation.data.get("is_genuine_attempt") is True
            quality = clamp(evaluation.data.get("answer_quality"), 0.0, 1.0, 0.5) or 0.0
            # the model may return any JSON shape ("scores": 72, a list, ...): never let that crash the turn
            scores = evaluation.data.get("scores")
            scores = scores if isinstance(scores, dict) else {}
            eval_meta = {"genuine": genuine, "quality": round(quality, 2), "confidence": round(evaluation.confidence, 3),
                         "low_confidence": evaluation.low_confidence, "samples": evaluation.samples,
                         "dimensions": sorted(k for k in scores if isinstance(k, str) and k in prompts.DIMENSIONS)}
            if genuine:
                length_factor = min(1.0, 0.4 + len(user_msg.content) / 400)
                weight = evaluation.confidence * length_factor * (0.6 if evaluation.low_confidence else 1.0)
                add_observations(s, user.id, scores, confidence=weight, source="chat", cap=0.6)
                if recall_target is not None and isinstance(evaluation.data.get("recall_correct"), bool):
                    correct = evaluation.data["recall_correct"]
                    s.add(Observation(user_id=user.id, dimension="recall", score=85.0 if correct else 25.0,
                                      confidence=min(0.6, evaluation.confidence), source="recall",
                                      evidence=("remembered" if correct else "did not remember") + f": {_trim(recall_target, 200)}"))
                gain = round(level.progress_gain * (0.35 + 0.65 * quality), 2)
        reply_msg = Message(
            user_id=user.id,
            role="assistant",
            content=reply,
            meta={"turn_type": plan_kind, "mood": mood, "question": question, "level": level.number,
                  "question_source": "qwen-coder" if coach is not None else "gateway", "code_check": code_check,
                  "progress_gain": gain, "context_tokens": eff_tokens, "eval": eval_meta, "spam_blocked": is_spam,
                  **plan_meta, **gate_meta},
        )
        s.add(reply_msg)
        await s.flush()
        events = await apply_progress(s, user, gain) if gain > 0 else []
        if callback is not None:
            memory = await s.get(Memory, callback.id)
            if memory is not None:
                memory.recall_count += 1
                memory.last_recalled_at = utcnow()
        if genuine and len(user_msg.content) >= 40:
            await remember(s, user.id, "answer", f"Student answered at level {level.number}: {user_msg.content}",
                           source_ref=f"msg:{user_msg.id}", importance=0.35 + 0.4 * quality)
        if question:
            await remember(s, user.id, "question", f"Tutor asked: {question}", source_ref=f"msg:{reply_msg.id}", importance=0.25)
        if plan_kind == "project" and plan_meta.get("project_id"):
            # locked (after the user row, as everywhere) so a parallel bug hunt's analysis update is not overwritten
            project = await s.get(Project, plan_meta["project_id"], with_for_update=True, populate_existing=True)
            if project is not None:
                json_set(project.analysis, "discussed", True)
        after = await observation_count(s, user.id)
        refreshed = await maybe_queue_profile_refresh(s, user.id, before, after)
        await s.commit()
    if refreshed:
        from .queue import queue

        await queue.notify()
    return JobOutcome(
        result={"reply_id": reply_msg.id, "events": events, "gain": gain, "plan": plan_kind, "spam_blocked": is_spam,
                "question_source": "qwen-coder" if coach is not None else "gateway"},
        confidence=gate_meta.get("confidence"),
        low_confidence=bool(gate_meta.get("low_confidence")),
        provider=str(gate_meta.get("provider") or tutor.provider),
    )


# --------------------------------------------------------------------------- profile refresh
@job_handler("profile_refresh")
async def profile_refresh(ctx: JobContext) -> JobOutcome:
    async with SessionLocal() as s:
        user = await load_user(s, ctx.user_id)
        summary = summarize((await s.execute(select(Observation).where(Observation.user_id == user.id))).scalars().all())
        projects = list((await s.execute(select(Project).where(Project.user_id == user.id))).scalars())
        challenges = list((await s.execute(
            select(Challenge).where(Challenge.user_id == user.id, Challenge.score.is_not(None)).order_by(Challenge.created_at.desc()).limit(10)
        )).scalars())
    level = get_level(normalize(user.progression)["level"])
    evidence = {
        "profile": profile_for_ai(user),
        "observation_summary": {k: {"score": d["score"], "status": d["status"], "observations": d["observations"]}
                                for k, d in summary["dimensions"].items()},
        "projects": [{"name": p.name, "level_estimate": ((p.analysis or {}).get("review") or {}).get("level_estimate"),
                      "ai_assistance": ((p.analysis or {}).get("ai_detection") or {}).get("likelihood")} for p in projects],
        "challenges": [{"kind": c.kind, "title": c.title, "level": c.level, "score": c.score} for c in challenges],
        "previous_swot_analysis": (user.cognitive or {}).get("swot_analysis"),
    }
    gate = await ask_json("gateway", prompts.SWOT_SYSTEM, "EVIDENCE:\n" + json.dumps(evidence, ensure_ascii=False)[:12000],
                          user=user, required=("swot_analysis",), temperature=0.3, max_tokens=1500,
                          samples=min(2, level.samples))
    async with SessionLocal() as s:
        user = await load_user(s, ctx.user_id, lock=True)
        json_set(user.cognitive, "swot_analysis", normalize_swot(gate.data.get("swot_analysis")))
        json_set(user.cognitive, "narrative", as_str(gate.data.get("narrative"), 2000))
        json_set(user.cognitive, "focus_next", as_list(gate.data.get("focus_next"), 6))
        json_set(user.cognitive, "insufficient_areas", as_list(gate.data.get("insufficient_areas"), 8))
        json_set(user.cognitive, "swot_meta", gate.meta())
        json_set(user.cognitive, "refreshed_at", utcnow().isoformat())
        await s.commit()
    return JobOutcome(result={"refreshed": True}, confidence=gate.confidence, low_confidence=gate.low_confidence, provider=gate.provider)


# --------------------------------------------------------------------------- OCR (chat attachment)
@job_handler("ocr_image")
async def ocr_image(ctx: JobContext) -> JobOutcome:
    path = Path(ctx.payload["path"]).resolve()
    if UPLOAD_DIR.resolve() not in path.parents:
        raise RuntimeError("invalid upload path")
    async with SessionLocal() as s:
        user = await load_user(s, ctx.user_id)
    try:
        data = await asyncio.to_thread(path.read_bytes)
        result = await run_ocr(data, user=user)
    finally:
        path.unlink(missing_ok=True)
    return JobOutcome(result=result, confidence=result["confidence"], low_confidence=result["low_confidence"], provider=str(result["engine"] or "ocr"))


@on_failure("ocr_image")
async def _ocr_failed(ctx: JobContext, error: str) -> None:
    """Halted / timed out / crashed before (or while) reading it: the temporary image is never needed again."""
    path = Path(str(ctx.payload.get("path", ""))).resolve()
    if UPLOAD_DIR.resolve() in path.parents:
        path.unlink(missing_ok=True)
