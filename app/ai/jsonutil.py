"""Robust extraction of a JSON object from LLM output."""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _balanced_object(text: str) -> str | None:
    """Return the first balanced {...} block, respecting JSON strings."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def _try_load(candidate: str) -> dict[str, Any] | None:
    for attempt in (candidate, _TRAILING_COMMA.sub(r"\1", candidate)):
        try:
            value = json.loads(attempt, strict=False)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def extract_json(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = _THINK.sub("", text).strip()
    direct = _try_load(cleaned)
    if direct is not None:
        return direct
    for block in _FENCE.findall(cleaned):
        parsed = _try_load(block.strip())
        if parsed is not None:
            return parsed
    candidate = _balanced_object(cleaned)
    if candidate:
        return _try_load(candidate)
    return None


def parse_confidence(value: Any) -> float | None:
    """Accept 0..1 floats, 0..100 numbers, or strings like '85%'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if not match:
            return {"high": 0.85, "medium": 0.6, "low": 0.3}.get(value.strip().lower())
        number = float(match.group(0))
        if "%" in value:
            number /= 100.0
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
    if number > 1.0:
        number /= 100.0
    return max(0.0, min(1.0, number))
