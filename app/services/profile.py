"""Cognitive-profile maths.

Scores are confidence-weighted Bayesian averages shrunk toward a neutral prior (50), so a
handful of chats cannot swing the profile - the app does not jump to conclusions. Each
dimension reports its evidence and is explicitly declared "needs more chatting" until
enough exists. The IQ figure is a playful, wide-range ESTIMATE that is only shown once
there is broad evidence, and is never a real or clinical IQ test.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from ..models import Observation

PRIOR_MEAN = 50.0
PRIOR_WEIGHT = 4.0
EMERGING_EVIDENCE = 3.0
ESTABLISHED_EVIDENCE = 8.0
IQ_MIN_OBSERVATIONS = 20
IQ_MIN_DIMENSIONS = 4
IQ_MIN_SOURCES = 2

DIMENSIONS: dict[str, str] = {
    "logic": "Logical reasoning",
    "analytical": "Analytical thinking",
    "critical": "Critical thinking",
    "metacognition": "Metacognition",
    "foresight": "Bug foresight (what-ifs)",
    "recall": "Memory recall",
}
COMPOSITE_WEIGHTS = {"logic": 1.2, "analytical": 1.2, "critical": 1.1, "metacognition": 1.0, "foresight": 1.0, "recall": 0.5}


def summarize(observations: Iterable[Observation]) -> dict[str, Any]:
    acc: dict[str, dict[str, Any]] = defaultdict(lambda: {"w": 0.0, "ws": 0.0, "n": 0, "sources": set(), "recent": []})
    all_sources: set[str] = set()
    total_n = 0
    total_w = 0.0
    for obs in sorted(observations, key=lambda o: o.created_at):
        if obs.dimension not in DIMENSIONS:
            continue
        weight = max(0.05, min(1.0, float(obs.confidence or 0)))
        score = max(0.0, min(100.0, float(obs.score)))
        bucket = acc[obs.dimension]
        bucket["w"] += weight
        bucket["ws"] += weight * score
        bucket["n"] += 1
        bucket["sources"].add(obs.source)
        bucket["recent"].append(score)
        all_sources.add(obs.source)
        total_n += 1
        total_w += weight

    dims: dict[str, Any] = {}
    for key, label in DIMENSIONS.items():
        b = acc.get(key) or {"w": 0.0, "ws": 0.0, "n": 0, "sources": set(), "recent": []}
        mean = (PRIOR_MEAN * PRIOR_WEIGHT + b["ws"]) / (PRIOR_WEIGHT + b["w"])
        margin = 25.0 / math.sqrt(1.0 + b["w"])
        if b["w"] < EMERGING_EVIDENCE:
            status, message = "insufficient", "Not enough evidence yet - more AI chatting is required before concluding."
        elif b["w"] < ESTABLISHED_EVIDENCE:
            status, message = "emerging", "Emerging picture - still uncertain, keep training."
        else:
            status, message = "established", "Reasonably established from repeated evidence."
        recent = b["recent"][-5:]
        older = b["recent"][:-5]
        trend = None
        if len(recent) >= 3 and len(older) >= 3:
            trend = round(sum(recent) / len(recent) - sum(older) / len(older), 1)
        dims[key] = {
            "key": key,
            "label": label,
            "score": round(mean, 1),
            "margin": round(margin, 1),
            "low": round(max(0.0, mean - margin), 1),
            "high": round(min(100.0, mean + margin), 1),
            "observations": b["n"],
            "evidence_weight": round(b["w"], 2),
            "sources": sorted(b["sources"]),
            "status": status,
            "message": message,
            "trend": trend,
        }

    ready_dims = [d for d in dims.values() if d["status"] != "insufficient"]
    composite = None
    if ready_dims:
        weight_sum = sum(COMPOSITE_WEIGHTS[d["key"]] for d in ready_dims)
        composite = sum(d["score"] * COMPOSITE_WEIGHTS[d["key"]] for d in ready_dims) / weight_sum

    iq_ready = total_n >= IQ_MIN_OBSERVATIONS and len(ready_dims) >= IQ_MIN_DIMENSIONS and len(all_sources) >= IQ_MIN_SOURCES
    if iq_ready and composite is not None:
        estimate = max(70.0, min(145.0, 100.0 + (composite - 50.0) * 0.6))
        spread = max(4.0, 15.0 / math.sqrt(1.0 + total_w / 12.0))
        iq = {
            "ready": True,
            "estimate": round(estimate),
            "low": round(max(55.0, estimate - spread)),
            "high": round(min(160.0, estimate + spread)),
            "message": "Playful estimate from training behaviour only - NOT an IQ test, not clinical, not medical.",
        }
    else:
        iq = {
            "ready": False,
            "estimate": None,
            "needed": {
                "observations": f"{total_n}/{IQ_MIN_OBSERVATIONS}",
                "dimensions_with_evidence": f"{len(ready_dims)}/{IQ_MIN_DIMENSIONS}",
                "evidence_sources": f"{len(all_sources)}/{IQ_MIN_SOURCES} (e.g. chat + challenges/projects)",
            },
            "message": "Not enough data yet - the AI will not guess. Keep chatting, solving challenges and sharing projects.",
        }
    return {
        "dimensions": dims,
        "composite": round(composite, 1) if composite is not None else None,
        "iq": iq,
        "total_observations": total_n,
        "sources": sorted(all_sources),
    }


def calibration_score(self_confidence: float | None, actual_score: float | None) -> float | None:
    """Metacognition signal: how well self-rated confidence matched the actual result (0-100)."""
    if self_confidence is None or actual_score is None:
        return None
    return max(0.0, 100.0 - abs(float(self_confidence) - float(actual_score)) * 1.2)
