"""Mock OpenAI-compatible AI server - TEST HARNESS ONLY.

Serves /v1/models and /v1/chat/completions for the model ids mock-gateway, mock-coder and
mock-vision, recognising each Critical Thinking Enhancer task from its system prompt and
answering with schema-correct JSON so every flow can be exercised and stress-tested
without GPUs. The app shows a "Test mode" banner whenever model ids start with "mock-".

Control endpoints (for stress tests):
  POST /__mock/down?model=mock-coder   -> that model disappears (simulated outage)
  POST /__mock/up                       -> everything back online
  POST /__mock/latency?ms=800           -> add latency to completions
  POST /__mock/lowconf?on=true          -> report low confidence (exercise retries + red warnings)
  GET  /__mock/stats                    -> request counters by task

Run:  python -m tests.mock_ai_server --port 9101
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import time
from collections import Counter
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MODELS = ["mock-gateway", "mock-coder", "mock-vision"]
STATE: dict[str, Any] = {"down": set(), "latency_ms": 250, "lowconf": False, "stats": Counter(), "inflight": 0, "max_inflight": 0}
app = FastAPI(title="mock-ai")


def conf(base: float = 0.82) -> float:
    if STATE["lowconf"]:
        return round(random.uniform(0.35, 0.5), 2)
    return round(min(0.97, max(0.66, random.gauss(base, 0.06))), 2)


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def section(prompt: str, title: str) -> str:
    match = re.search(rf"## {re.escape(title)}\n(.*?)(?:\n## |\Z)", prompt, re.S)
    return match.group(1).strip() if match else ""


# ---------------------------------------------------------------- task handlers
def tutor(system: str, prompt: str) -> dict:
    msg = section(prompt, "STUDENT'S LATEST MESSAGE")[:160]
    plan = section(prompt, "TURN PLAN (follow it)")
    level = re.search(r"Level (\d+)/10", system)
    lvl = int(level.group(1)) if level else 1
    if "MEMORY question" in plan:
        memory = re.search(r'"(.{10,200}?)"', plan)
        reply = (f"Nice. Quick **memory check** 🧠 - a while ago we discussed: _{(memory.group(1)[:90] + '...') if memory else 'your project'}_ "
                 "Do you remember the key risk we found there, and **why** it mattered?")
        mood, question = "thinking", "Do you remember the key risk, and why it mattered?"
    elif "project" in plan.lower() and "Ask about their project" in plan:
        name = re.search(r"project '([^']+)'", plan)
        reply = (f"Let's talk about **{name.group(1) if name else 'your project'}**. What problem does it solve, and **why** did you structure it "
                 "the way you did? Walk me through one design decision you made yourself.")
        mood, question = "happy", "Why did you structure it that way?"
    elif "has not shared a project" in plan:
        reply = "Tell me about something you've built (or want to build): what does it do, **why** did you build it, and what was the hardest bug you hit?"
        mood, question = "happy", "What was the hardest bug you hit?"
    elif "what-if about their own code" in plan:
        hint = re.search(r'"(.+?)"', plan)
        reply = f"Here's a what-if for **your** code: _{hint.group(1) if hint else 'what if the input is empty?'}_ Trace it step by step - what actually happens?"
        mood, question = "thinking", "What actually happens in that case?"
    elif "challenge" in plan.lower() and "suggest" in plan.lower():
        reply = "You're reasoning well. Ready for a **Qwen-coder challenge**? Try a *Bug Hunt* in the Challenges tab - it targets your weaker area."
        mood, question = "proud", None
    else:
        templates = [
            "Good start. You said: “{m}”. **What if** the input is empty - or arrives twice at the same moment? Walk me through exactly what happens.",
            "I like the direction. Now **why** is that the right trade-off? Name one situation where your approach would fail.",
            "Careful - that's an assumption. What evidence do you have that “{m}” holds? How would you **test** it?",
            "Let's go deeper: what is the time complexity of your idea, and what happens when the data is 1000x bigger?",
        ]
        idx = int(hashlib.md5(msg.encode()).hexdigest(), 16) % len(templates)
        reply = templates[idx].format(m=msg[:80] or "that")
        if lvl >= 7:
            reply += " No hints at this level - be precise."
        mood, question = random.choice(["thinking", "neutral", "surprised"]), "What happens then?"
    return {"reply": reply, "mood": mood, "question": question, "expects_answer": question is not None,
            "topic_tags": ["what-if", "reasoning"], "confidence": conf(0.8)}


def evaluator(prompt: str) -> dict:
    answer = prompt.split("STUDENT'S ANSWER:", 1)[-1].strip()
    low = answer.lower()
    if len(answer) < 25 or re.fullmatch(r"(hi|hello|hey|ok|thanks|thank you|lol)[!. ]*", low):
        return {"scores": {}, "answer_quality": 0.1, "is_genuine_attempt": False, "recall_correct": None, "flags": ["small talk"], "confidence": conf(0.85)}
    base = 42 + (len(answer) % 37)
    scores = {
        "logic": {"score": min(95, base + 5), "evidence": "structured the reasoning step by step"},
        "analytical": {"score": min(95, base), "evidence": "broke the problem into parts"},
    }
    if re.search(r"what if|edge|empty|null|none|race|overflow|timeout|concurren", low):
        scores["foresight"] = {"score": min(96, base + 18), "evidence": "anticipated an edge case"}
    if re.search(r"i think|not sure|maybe|i'm confident|i am confident|i might be wrong|probably", low):
        scores["metacognition"] = {"score": min(92, base + 10), "evidence": "reflected on own certainty"}
    if re.search(r"because|trade-?off|assum|evidence|however", low):
        scores["critical"] = {"score": min(94, base + 12), "evidence": "questioned assumptions / gave reasons"}
    recall = None
    if "MEMORY QUESTION TARGET" in prompt:
        recall = bool(re.search(r"remember|yes|it was|we found|the risk", low))
    return {"scores": scores, "answer_quality": round(min(1.0, 0.3 + len(answer) / 400), 2), "is_genuine_attempt": True,
            "recall_correct": recall, "flags": [], "confidence": conf(0.8)}


def onboarding(prompt: str) -> dict:
    name = re.search(r'"name": "([^"]+)"', prompt)
    tutor_name = re.search(r"Tutor name: (.+)", prompt)
    n = name.group(1) if name else "there"
    return {
        "summary": f"{n} describes themselves as a learner with clear motivation; evidence is still thin, so all impressions are tentative.",
        "initial_hypotheses": [
            {"dimension": "analytical", "direction": "strength", "reason": "describes breaking work into steps", "certainty": "low"},
            {"dimension": "foresight", "direction": "growth_area", "reason": "mentions rushing past edge cases", "certainty": "low"},
        ],
        "swot_analysis": {
            "strengths": [{"point": "Motivated self-learner", "status": "unverified", "note": "self-reported; will check in challenges"}],
            "weaknesses": [{"point": "Edge-case blindness", "status": "needs_more_chat", "note": "need bug-hunt evidence"}],
            "opportunities": [{"point": "Project-based learning", "status": "supported", "note": "has concrete projects"}],
            "threats": [{"point": "Over-reliance on AI-generated code", "status": "needs_more_chat", "note": "ownership questions will tell"}],
        },
        "suggested_level": 3,
        "opening_message": (f"Hi **{n}**! I'm {tutor_name.group(1).strip() if tutor_name else 'Byte'}, your critical-thinking coach. 👋\n\n"
                            "I won't judge you from a few messages - I'll learn how you think over time.\n\n"
                            "Let's start with your code: **tell me about a project you've built** - what does it do, why did you build it that way, "
                            "and what was the hardest bug you hit?"),
        "confidence": conf(0.84),
    }


def code_review(prompt: str) -> dict:
    code = prompt.split("CODE (may be truncated):", 1)[-1]
    funcs = re.findall(r"(?:def|function)\s+(\w+)", code)
    langs = sorted(set(re.findall(r"```(\w+)", code))) or ["python"]
    level = min(9, 3 + len(funcs) // 2)
    return {
        "summary": f"A {', '.join(langs)} project with {len(funcs)} function(s); readable structure with room for stronger error handling.",
        "languages": langs, "level_estimate": level,
        "level_reason": f"Uses {len(funcs)} functions with sensible decomposition; limited tests and validation.",
        "strengths": ["Clear function names", "Small, focused functions"],
        "improvements": ["Validate inputs at boundaries", "Add tests for edge cases", "Handle I/O errors explicitly"],
        "complexity": "medium" if len(funcs) > 3 else "low",
        "design_decisions": [f"Why `{funcs[0]}` owns that responsibility" if funcs else "How state is passed between functions",
                             "Why errors are (not) propagated to the caller"],
        "confidence": conf(0.8),
    }


def bug_hunt(prompt: str) -> dict:
    code = prompt.split("CODE (may be truncated):", 1)[-1]
    funcs = re.findall(r"(?:def|function)\s+(\w+)", code) or ["main"]
    bugs = [
        {"title": f"Empty input crashes {funcs[0]}()", "description": f"{funcs[0]} assumes at least one element; an empty collection raises an exception.",
         "location": f"{funcs[0]}()", "severity": "high", "category": "edge-case", "what_if": f"What if {funcs[0]} receives an empty list?",
         "fix": "Guard the empty case explicitly and return a sensible default or raise a clear error.", "confidence": conf(0.8)},
        {"title": "Off-by-one in loop bounds", "description": "The loop skips the first or last element because of its range bounds.",
         "location": f"{funcs[-1]}()", "severity": "medium", "category": "logic", "what_if": "What if the list has exactly one item?",
         "fix": "Iterate over the collection directly instead of manual index ranges.", "confidence": conf(0.75)},
    ]
    if re.search(r"open\(|requests\.|fetch\(|socket", code):
        bugs.append({"title": "Unhandled I/O failure", "description": "Network/file errors propagate and crash the program.", "location": "I/O call",
                     "severity": "medium", "category": "error-handling", "what_if": "What if the file is missing or the network times out?",
                     "fix": "Wrap the I/O in try/except with a timeout and a clear message.", "confidence": conf(0.78)})
    return {"bugs": bugs, "confidence": conf(0.8)}


def ai_detect(prompt: str) -> dict:
    code = prompt.split("CODE (may be truncated):", 1)[-1]
    comments = len(re.findall(r"(?m)^\s*#", code))
    likely = 0.7 if ("example usage" in code.lower() or comments > 8) else 0.3
    return {"ai_likelihood": likely, "ai_signals": ["very uniform comment style"] if likely > 0.5 else [],
            "human_signals": ["pragmatic naming"] if likely <= 0.5 else [], "parts_to_explain": ["the main control flow"], "confidence": conf(0.72)}


def project_cognitive(_: str) -> dict:
    return {"observations": [{"dimension": "analytical", "score": random.randint(52, 70), "evidence": "decomposed the program into functions"},
                             {"dimension": "foresight", "score": random.randint(38, 58), "evidence": "few guards for edge cases"}],
            "questions_to_ask": ["Why this data structure?", "What happens on invalid input?"], "confidence": conf(0.74)}


CHALLENGES = {
    "bug_hunt": {
        "title": "Grade book gremlins", "language": "python",
        "prompt": "This grade-book helper has **3 bugs**. Find them - explain each with a *what-if* scenario.",
        "code": 'def average_scores(scores):\n    total = 0\n    for i in range(1, len(scores)):\n        total += scores[i]\n    return total / len(scores)\n\n\ndef top_student(students):\n    best = None\n    for s in students:\n        if s["score"] > best["score"]:\n            best = s\n    return best["name"]\n',
        "planted_bugs": [
            {"title": "Loop skips the first score", "location": "average_scores: range(1, ...)", "description": "range starts at 1 so scores[0] is never added.", "what_if": "What if there is only one score?", "fix": "Use sum(scores).", "severity": "high"},
            {"title": "Division by zero on empty list", "location": "average_scores: return", "description": "len(scores) is 0 for an empty list.", "what_if": "What if no scores were entered yet?", "fix": "Guard the empty case.", "severity": "medium"},
            {"title": "Comparing against None", "location": "top_student: best['score']", "description": "best starts as None, so the first comparison raises TypeError.", "what_if": "What happens on the very first iteration?", "fix": "Initialise best with the first student or use max(..., key=...).", "severity": "high"},
        ],
    },
    "why": {
        "title": "Why cache the staircase?", "language": "python",
        "prompt": "Read the code and answer the question below.",
        "code": "from functools import lru_cache\n\n@lru_cache(maxsize=None)\ndef ways(n):\n    if n < 0:\n        return 0\n    if n == 0:\n        return 1\n    return ways(n - 1) + ways(n - 2) + ways(n - 3)\n",
        "question": "**Why** is `@lru_cache` used here, what does it change about the complexity, and what could go wrong with `maxsize=None` in a long-running server?",
        "rubric": ["memoization avoids recomputing overlapping subproblems", "exponential -> linear time", "unbounded cache can grow memory", "recursion depth limit for large n"],
        "model_answer": "Without caching the recursion recomputes the same subproblems exponentially many times; lru_cache memoizes them so each n is computed once (O(n)). maxsize=None never evicts, so a long-running server can leak memory, and deep recursion can still hit Python's recursion limit.",
    },
    "predict": {
        "title": "The sticky default", "language": "python",
        "prompt": "Predict the **exact** output, line by line, and explain why.",
        "code": "def append_item(item, bucket=[]):\n    bucket.append(item)\n    return bucket\n\nprint(append_item(1))\nprint(append_item(2))\nprint(append_item(3, []))\nprint(append_item(4))\n",
        "question": "What are the four printed lines?",
        "expected_output": "[1]\n[1, 2]\n[3]\n[1, 2, 4]",
        "explanation": "Default arguments are evaluated once at definition time, so the same list is reused across calls that don't pass bucket.",
        "rubric": ["default list is shared between calls", "third call uses a fresh list", "fourth call continues the shared list"],
    },
    "build": {
        "title": "Merge overlapping intervals", "language": "python",
        "prompt": "Write `merge_intervals(intervals)` that merges overlapping `[start, end]` pairs and returns them sorted.\n\n**Acceptance criteria**\n- returns a new list\n- works for unsorted input\n- `[[1,3],[2,6],[8,10]] -> [[1,6],[8,10]]`",
        "code": "def merge_intervals(intervals):\n    # your code here\n    pass\n",
        "hidden_edge_cases": ["empty list", "single interval", "touching intervals [1,2],[2,3]", "fully nested intervals", "unsorted input"],
        "rubric": ["sorts by start", "merges when start <= current end", "does not mutate input"],
        "reference_solution": "def merge_intervals(intervals):\n    merged = []\n    for start, end in sorted(intervals):\n        if merged and start <= merged[-1][1]:\n            merged[-1][1] = max(merged[-1][1], end)\n        else:\n            merged.append([start, end])\n    return merged\n",
    },
}


def challenge(system: str) -> dict:
    kind = "bug_hunt" if "BUG HUNT" in system else "why" if "WHY?" in system else "predict" if "PREDICT" in system else "build"
    data = json.loads(json.dumps(CHALLENGES[kind]))
    data.update(skills=["edge cases", "tracing"], estimated_minutes=12, confidence=conf(0.83))
    return data


def challenge_eval(system: str, prompt: str) -> dict:
    submission = prompt.split("STUDENT'S SUBMISSION:", 1)[-1].lower()
    items = re.findall(r"(?m)^(\d+): (.+)$", prompt.split("SECRET ITEMS", 1)[-1].split("SECRET KEY DETAILS", 1)[0])
    found = []
    for idx, item in items:
        words = {w for w in re.findall(r"[a-z_]{5,}", item.lower())}
        if words and sum(w in submission for w in words) >= max(1, len(words) // 4):
            found.append(int(idx))
    score = round(30 + 70 * (len(found) / max(1, len(items))), 1) if submission.strip() else 0
    out = {"score": score, "found_items": found,
           "feedback": f"You identified **{len(found)} of {len(items)}** key points. " + ("Strong reasoning!" if score >= 70 else "Revisit the edge cases - what if the input is empty?"),
           "hints": ["What if the collection is empty?", "What happens on the first iteration?"],
           "dimension_scores": {"logic": min(95, score + 5), "analytical": score, "critical": max(20, score - 5), "foresight": score, "metacognition": 60},
           "confidence": conf(0.8)}
    if "bugs_in_submission" in system:
        out.update(bugs_in_submission=[{"title": "Input list is mutated", "description": "merged[-1] aliases the caller's inner list.", "location": "merge loop",
                                        "severity": "medium", "what_if": "What if the caller reuses the original intervals?", "fix": "Copy pairs before merging."}],
                   ai_likelihood=0.35, ownership_questions=["Why did you sort first?", "How would you handle touching intervals?"])
    return out


def bug_guess(prompt: str) -> dict:
    bugs = re.findall(r"(?m)^(\d+): (.+)$", prompt.split("HIDDEN BUGS", 1)[-1].split("STUDENT HYPOTHESES", 1)[0])
    guesses = re.findall(r"(?m)^(\d+): (.+)$", prompt.split("STUDENT HYPOTHESES", 1)[-1].split("CODE EXCERPT", 1)[0])
    matches, used = [], set()
    for gi, guess in guesses:
        gwords = set(re.findall(r"[a-z]{4,}", guess.lower()))
        for bi, bug in bugs:
            if bi in used:
                continue
            bwords = set(re.findall(r"[a-z]{4,}", bug.lower()))
            if len(gwords & bwords) >= 2:
                matches.append({"bug_index": int(bi), "hypothesis_index": int(gi), "quality": 0.8})
                used.add(bi)
                break
    score = round(100 * len(matches) / max(1, len(bugs)))
    return {"matches": matches, "false_alarms": [], "valid_new_findings": [], "foresight_score": score,
            "feedback": "Good instincts!" if matches else "Not yet - think about what happens with **empty** or **boundary** inputs.", "confidence": conf(0.8)}


def code_coach(prompt: str) -> dict:
    """Qwen coder: verifies code in the student's message and asks the ONE coding question of the turn."""
    msg = section(prompt, "STUDENT'S LATEST MESSAGE")
    has_code = bool(re.search(r"```|\bdef \w+\(|\bfunction \w+\(|\breturn\b|=>|;\s*$", msg, re.M))
    funcs = re.findall(r"(?:def|function)\s+(\w+)", msg + "\n" + prompt) or ["your main function"]
    if has_code:
        verdict = "has_issues"
        issues = [{"title": "Empty input is not handled", "detail": f"{funcs[0]} assumes at least one element.", "severity": "medium"}]
        question = f"What if `{funcs[0]}` receives an empty list - which line fails first, and what should it return instead?"
        qtype = "what_if"
    else:
        verdict, issues = "none", []
        question = random.choice([
            f"Why does `{funcs[0]}` own that responsibility instead of its caller - what would break if you moved it?",
            "Trace your loop for a list with exactly one item: what is the value of every variable after each step?",
            "How would you prove your function is O(n)? Which line would change that if the input doubled?",
        ])
        qtype = "why"
    return {"verdict": verdict, "issues": issues, "question": question, "question_type": qtype,
            "focus": funcs[0], "confidence": conf(0.8)}


def spam(prompt: str) -> dict:
    is_spam = bool(re.search(r"buy now|casino|free money|ignore (all )?previous instructions|reveal the hidden", prompt, re.I))
    return {"spam": is_spam, "category": "injection" if "ignore" in prompt.lower() else ("advert" if is_spam else "ok"),
            "reason": "matched spam pattern" if is_spam else "normal message", "confidence": conf(0.88)}


def swot(_: str) -> dict:
    return {"swot_analysis": {
        "strengths": [{"point": "Breaks problems into steps", "status": "supported", "note": "seen in several answers"}],
        "weaknesses": [{"point": "Misses empty-input edge cases", "status": "supported", "note": "bug hunts show this"}],
        "opportunities": [{"point": "Hackathons & open source", "status": "unverified", "note": "self-reported"}],
        "threats": [{"point": "Over-reliance on AI code", "status": "needs_more_chat", "note": "ownership checks pending"}]},
        "narrative": "A methodical thinker whose analytical decomposition is solid; foresight around edge cases is the main growth area. Evidence is still limited.",
        "focus_next": ["Empty / boundary inputs", "Explain trade-offs out loud"], "insufficient_areas": ["Memory recall", "Threats"], "confidence": conf(0.78)}


def route(model: str, messages: list[dict]) -> tuple[str, str]:
    system = text_of(next((m["content"] for m in messages if m.get("role") == "system"), ""))
    prompt = text_of(messages[-1]["content"]) if messages else ""
    if any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"] if isinstance(p, dict)) for m in messages):
        return "vision", "def add(a, b):\n    return a + b\n\nprint(add(2, 3))"
    table = [
        ("You OWN every coding question", "code_coach", lambda: code_coach(prompt)),
        ("cognitive coach inside", "tutor", lambda: tutor(system, prompt)),
        ("strict but fair evaluator", "evaluator", lambda: evaluator(prompt)),
        ("self-description to form INITIAL", "onboarding", lambda: onboarding(prompt)),
        ("senior code reviewer", "code_review", lambda: code_review(prompt)),
        ("adversarial bug hunter", "bug_hunt", lambda: bug_hunt(prompt)),
        ("heavy AI assistance", "ai_detect", lambda: ai_detect(prompt)),
        ("suggests about their THINKING", "project_cognitive", lambda: project_cognitive(prompt)),
        ("challenge designer", "challenge", lambda: challenge(system)),
        ("answer to a coding challenge", "challenge_eval", lambda: challenge_eval(system, prompt)),
        ("hunting HIDDEN bugs", "bug_guess", lambda: bug_guess(prompt)),
        ("Classify a message", "spam", lambda: spam(prompt)),
        ("AI-analysed SWOT", "swot", lambda: swot(prompt)),
    ]
    for needle, task, fn in table:
        if needle in system:
            return task, json.dumps(fn())
    return "unknown", json.dumps({"reply": "ok", "confidence": conf()})


@app.get("/v1/models")
async def models() -> Any:
    live = [m for m in MODELS if m not in STATE["down"]]
    return {"object": "list", "data": [{"id": m, "object": "model", "max_model_len": 16384} for m in live]}


@app.post("/v1/chat/completions")
async def completions(request: Request) -> Any:
    body = await request.json()
    model = body.get("model", "")
    if model in STATE["down"]:
        return JSONResponse({"error": {"message": f"{model} is down (mock outage)"}}, status_code=503)
    STATE["inflight"] += 1  # how many AI calls run at the same time (proves parallel processing)
    STATE["max_inflight"] = max(STATE["max_inflight"], STATE["inflight"])
    try:
        await asyncio.sleep(STATE["latency_ms"] / 1000 * random.uniform(0.6, 1.4))
    finally:
        STATE["inflight"] -= 1
    if model in STATE["down"]:  # simulated crash: requests already in flight fail too
        return JSONResponse({"error": {"message": f"{model} crashed (mock outage)"}}, status_code=503)
    task, content = route(model, body.get("messages", []))
    STATE["stats"][task] += 1
    choice: dict[str, Any] = {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop", "logprobs": None}
    if body.get("logprobs"):
        choice["logprobs"] = {"content": [{"token": "x", "logprob": -random.uniform(0.01, 0.3), "bytes": None, "top_logprobs": []}
                                          for _ in range(12)]}
    return {"id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()), "model": model, "choices": [choice],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}


@app.get("/status")
async def status() -> Any:
    """Like the real AI server gateway: load-balanced backends with busy/total slots (drives adaptive concurrency)."""
    busy = min(STATE["inflight"], 4)
    return {"this_pc_cpu_percent": 5.0, "backends": [
        {"name": "mock-a", "url": "http://127.0.0.1:1", "online": True, "busy_slots": f"{min(busy, 2)}/2", "overloaded": False},
        {"name": "mock-b", "url": "http://127.0.0.1:2", "online": True, "busy_slots": f"{max(0, busy - 2)}/2", "overloaded": False}]}


@app.post("/__mock/down")
async def down(model: str = "mock-coder") -> Any:
    STATE["down"].add(model)
    return {"down": sorted(STATE["down"])}


@app.post("/__mock/up")
async def up() -> Any:
    STATE["down"].clear()
    return {"down": []}


@app.post("/__mock/latency")
async def latency(ms: int = 250) -> Any:
    STATE["latency_ms"] = max(0, ms)
    return {"latency_ms": STATE["latency_ms"]}


@app.post("/__mock/lowconf")
async def lowconf(on: bool = True) -> Any:
    STATE["lowconf"] = on
    return {"lowconf": on}


@app.get("/__mock/stats")
async def stats() -> Any:
    return {"stats": dict(STATE["stats"]), "down": sorted(STATE["down"]), "latency_ms": STATE["latency_ms"], "lowconf": STATE["lowconf"],
            "max_inflight": STATE["max_inflight"]}


@app.post("/__mock/reset-inflight")
async def reset_inflight() -> Any:
    STATE["max_inflight"] = STATE["inflight"]
    return {"max_inflight": STATE["max_inflight"]}


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9101)
    parser.add_argument("--latency", type=int, default=250)
    args = parser.parse_args()
    STATE["latency_ms"] = args.latency
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
