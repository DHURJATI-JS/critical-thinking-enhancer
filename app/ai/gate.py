"""Confidence gate.

Every AI conclusion must carry a confidence. Results are accepted only when the effective
confidence is >= ``ai.confidence_threshold`` (65 % by default). Otherwise the request is
retried with a "second look" instruction, more tokens and lower temperature. If the bar is
still not met the best attempt is returned flagged ``low_confidence`` so the UI shows it
with a red warning sign.

Effective confidence = model self-report, halved when required fields are missing, blended
with token-level probability when the server exposes logprobs, and scaled by agreement
between parallel samples at higher levels (self-consistency).
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ..config import get_settings
from ..models import User
from . import router
from .jsonutil import extract_json, parse_confidence
from .providers import ChatResult

JSON_RULES = (
    "\n\nOUTPUT FORMAT: respond with ONE valid JSON object and nothing else (no markdown fences). "
    'Always include "confidence": a number between 0 and 1 that honestly reflects how strongly the '
    "available evidence supports your conclusions. Thin evidence means low confidence."
)
RETRY_NOTE = (
    "\n\n[Second look] Your previous answer was not confident enough ({prev:.0%}). Re-examine the evidence, "
    "reason step by step privately, correct mistakes and answer again. Keep confidence honest: if the "
    "evidence is insufficient, say so explicitly inside the JSON fields."
)


@dataclass
class GateResult:
    data: dict[str, Any]
    confidence: float
    low_confidence: bool
    attempts: int
    samples: int
    provider: str
    model: str
    raw: str
    threshold: float

    def meta(self) -> dict[str, Any]:
        return {
            "confidence": round(self.confidence, 3),
            "low_confidence": self.low_confidence,
            "attempts": self.attempts,
            "samples": self.samples,
            "provider": self.provider,
            "model": self.model,
            "threshold": self.threshold,
        }


def effective_confidence(data: dict[str, Any] | None, token_conf: float | None, required: tuple[str, ...]) -> float:
    if not data:
        return 0.0
    conf = parse_confidence(data.get("confidence"))
    if conf is None:
        conf = 0.5
    if any(data.get(k) in (None, "", [], {}) for k in required):
        conf *= 0.5
    if token_conf is not None:
        conf = 0.8 * conf + 0.2 * token_conf
    return max(0.0, min(1.0, conf))


# Value range per self-consistency key: "level 1 vs level 10" must count as total disagreement, not 9 %.
AGREEMENT_SPANS: dict[str, float] = {"level_estimate": 9.0, "answer_quality": 1.0, "foresight_score": 100.0, "score": 100.0}


def agreement(values: list[Any], span: float | None = None) -> float:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return 1.0
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        nums = [float(v) for v in vals]
        if span is None:  # unknown key: guess the scale (0..1 or 0..100)
            span = 100.0 if max(abs(n) for n in nums) > 1.0 else 1.0
        elif span == 1.0:  # a 0..1 key where one sample answered on a 0..100 scale
            nums = [n / 100.0 if n > 1.0 else n for n in nums]
        return max(0.0, 1.0 - (max(nums) - min(nums)) / span)
    keys = [v.strip().lower() if isinstance(v, str) else json.dumps(v, sort_keys=True) for v in vals]
    return Counter(keys).most_common(1)[0][1] / len(keys)


async def ask_json(
    role: str,
    system: str,
    prompt: str,
    *,
    user: User | None = None,
    history: list[dict[str, Any]] | None = None,
    required: tuple[str, ...] = (),
    temperature: float = 0.4,
    max_tokens: int = 900,
    samples: int = 1,
    agreement_key: str | None = None,
    reasoning_effort: str | None = None,
    need_tokens: int = 0,
    threshold: float | None = None,
    max_retries: int | None = None,
    images: list[str] | None = None,
) -> GateResult:
    settings = get_settings()
    threshold = settings.ai.confidence_threshold if threshold is None else threshold
    max_retries = settings.ai.confidence_max_retries if max_retries is None else max_retries
    best: GateResult | None = None
    previous = 0.0
    attempts_made = 0
    for attempt in range(max_retries + 1):
        text = prompt if attempt == 0 else prompt + RETRY_NOTE.format(prev=previous)
        content: Any = text
        if images:
            content = [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": u}} for u in images]
        messages = [{"role": "system", "content": system + JSON_RULES}, *(history or []), {"role": "user", "content": content}]
        n = max(1, samples)
        temp = max(0.1, temperature - 0.15 * attempt) + (0.1 if n > 1 else 0.0)
        effort = reasoning_effort or ("medium" if attempt else None)
        calls = [
            router.chat(
                role,
                messages,
                user=user,
                need_tokens=need_tokens,
                temperature=temp,
                max_tokens=int(max_tokens * (1 + 0.3 * attempt)),
                json_mode=True,
                reasoning_effort=effort,
            )
            for _ in range(n)
        ]
        results = await asyncio.gather(*calls, return_exceptions=True)
        attempts_made = attempt + 1
        ok = [r for r in results if isinstance(r, ChatResult)]
        if not ok:
            if best is not None:  # keep the best earlier answer instead of failing the whole task
                break
            raise next(r for r in results if isinstance(r, BaseException))
        scored = []
        for res in ok:
            data = extract_json(res.text)
            scored.append((res, data, effective_confidence(data, res.token_confidence, required)))
        top_res, top_data, top_conf = max(scored, key=lambda item: item[2])
        if len(scored) > 1:
            mean_conf = sum(item[2] for item in scored) / len(scored)
            agree = agreement([(d if isinstance(d, dict) else {}).get(agreement_key) for _, d, _ in scored],
                              AGREEMENT_SPANS.get(agreement_key)) if agreement_key else 1.0
            conf = mean_conf * (0.5 + 0.5 * agree)
        else:
            conf = top_conf
        candidate = GateResult(
            data=top_data or {},
            confidence=conf,
            low_confidence=conf < threshold,
            attempts=attempts_made,
            samples=len(scored),
            provider=top_res.provider,
            model=top_res.model,
            raw=top_res.text,
            threshold=threshold,
        )
        if best is None or candidate.confidence >= best.confidence:
            best = candidate
        if conf >= threshold:
            break
        previous = conf
    assert best is not None
    best.attempts = attempts_made
    best.low_confidence = best.confidence < threshold
    return best
