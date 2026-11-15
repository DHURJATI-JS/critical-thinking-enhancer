"""Transparent heuristics for "was this code written with heavy AI assistance?".

Combined with the Qwen coder's judgement. This is never used to penalise the student: the
app only uses it to decide which parts to ask the student to explain (idea ownership).
"""
from __future__ import annotations

import re
from typing import Any

_NARRATING = re.compile(
    r"(?im)^\s*(?:#|//|--)\s*(?:step \d+|initiali[sz]e|create an?|define (?:the|a)|import (?:the )?necessary|check if|"
    r"loop (?:through|over)|iterate (?:through|over)|return the|this function|helper function|main function|"
    r"example usage|calculate the|set up|handle (?:the )?error)"
)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2705\u274C\u26A0\u2728\u2B50]")
_PLACEHOLDER = re.compile(r"(?i)your[_\- ]?(?:api[_\- ]?key|token|password|secret)[_\- ]?here|replace (?:this )?with your|<your[_\- ]")
_GOOGLE_DOC = re.compile(r"(?m)^\s*(?:Args|Returns|Raises|Parameters|Yields):\s*$")
_GENERIC_EXCEPT = re.compile(r"(?i)except Exception as e:\s*\n\s*(?:print|logger\.error|logging\.error)\(f?[\"']an? (?:error|exception)")
_TODO = re.compile(r"(?i)\b(?:TODO|FIXME|XXX|HACK)\b")
_DEBUG_PRINT = re.compile(r"(?i)(?:print|console\.log)\(\s*[\"'](?:here|test|debug|asdf|works|lol|ok)\b")
_PY_DEF = re.compile(r"(?m)^\s*(?:async\s+)?def\s+\w+\s*\(")
_PY_DOCSTRING_AFTER_DEF = re.compile(r"(?m)^\s*(?:async\s+)?def\s+\w+\s*\([^)]*\)[^:]*:\s*\n\s*(?:\"\"\"|''')")
_COMMENTED_CODE = re.compile(r"(?m)^\s*(?:#|//)\s*(?:\w+\s*=|print\(|return\b|if\b.*:|for\b.*:|console\.log)")


def heuristic_ai_likelihood(code: str) -> dict[str, Any]:
    lines = [ln for ln in code.splitlines() if ln.strip()]
    if len(lines) < 5:
        return {"likelihood": None, "ai_signals": [], "human_signals": [], "note": "too little code to judge"}
    ai: list[str] = []
    human: list[str] = []
    score = 0.25

    narrating = len(_NARRATING.findall(code))
    if narrating >= 3:
        score += min(0.22, 0.05 * narrating)
        ai.append(f"{narrating} comments narrate obvious steps")
    if re.search(r"(?i)example usage|# usage:", code):
        score += 0.08
        ai.append("'example usage' block")
    if _EMOJI.search(code):
        score += 0.12
        ai.append("emoji in comments / log messages")
    if _PLACEHOLDER.search(code):
        score += 0.10
        ai.append("'your API key here' style placeholders")
    defs = len(_PY_DEF.findall(code))
    if defs >= 3:
        ratio = len(_PY_DOCSTRING_AFTER_DEF.findall(code)) / defs
        if ratio >= 0.8:
            score += 0.12
            ai.append(f"docstrings on {ratio:.0%} of functions")
    if len(_GOOGLE_DOC.findall(code)) >= 3:
        score += 0.08
        ai.append("uniform Google-style Args/Returns sections")
    if len(_GENERIC_EXCEPT.findall(code)) >= 2:
        score += 0.07
        ai.append("generic 'An error occurred' exception boilerplate")

    if _TODO.search(code):
        score -= 0.08
        human.append("TODO / FIXME notes")
    if _DEBUG_PRINT.search(code):
        score -= 0.08
        human.append("debugging leftovers")
    if len(_COMMENTED_CODE.findall(code)) >= 3:
        score -= 0.06
        human.append("commented-out experiments")
    if any(ln.startswith("\t") for ln in lines) and any(ln.startswith("    ") for ln in lines):
        score -= 0.05
        human.append("mixed tabs and spaces")

    return {
        "likelihood": round(max(0.02, min(0.98, score)), 2),
        "ai_signals": ai,
        "human_signals": human,
        "note": "heuristic estimate - AI assistance is fine; ownership of the idea is what matters",
    }


def combine(heuristic: float | None, model: float | None, model_confidence: float) -> float | None:
    if heuristic is None and model is None:
        return None
    if model is None:
        return heuristic
    if heuristic is None:
        return model
    weight = 0.65 if model_confidence >= 0.65 else 0.4
    return round(weight * model + (1 - weight) * heuristic, 2)
