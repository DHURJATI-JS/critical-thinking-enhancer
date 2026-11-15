"""Scrutiny levels 1-10.

As the level rises the AI scrutinises harder AND thinks harder: more recent messages and
more pgvector memories in context (bigger context-memory budget), more self-consistency
samples for evaluations, larger reasoning budgets, fewer hints and more memory callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Level:
    number: int
    name: str
    scrutiny: str
    rules: str
    history: int  # recent chat messages kept in context
    recalls: int  # long-term memories (pgvector) pulled into context
    context_tokens: int  # context-memory budget requested at this level
    max_tokens: int  # reply budget
    samples: int  # parallel self-consistency samples for evaluations
    temperature: float
    recall_probability: float  # chance a turn becomes a "do you remember...?" callback
    max_words: int
    bugs_per_challenge: int
    reasoning_effort: str

    @property
    def progress_gain(self) -> float:
        return round(14.0 - 0.6 * (self.number - 1), 2)


_RULES = {
    1: "Be warm and encouraging. Give hints freely. Ask about ONE simple edge case at a time (empty input, zero, off-by-one). Build on partially-correct answers.",
    2: "Be encouraging. Offer a hint when the student is stuck. Ask simple 'what if' questions about inputs and outputs.",
    3: "Ask the student to justify claims briefly. One follow-up what-if per answer. Hints only after a genuine attempt.",
    4: "Point out vague answers and ask for specifics. Explore error handling and boundary conditions. Hints only on request.",
    5: "Challenge assumptions. Ask about time/space complexity, failure modes and trade-offs. No hints unless asked twice.",
    6: "Probe design decisions and alternatives. Ask how the code behaves with unusual input sizes, partial failures and changing requirements.",
    7: "Be skeptical and precise. Demand evidence or a test for every claim. Ask about concurrency, security and data integrity. No hints.",
    8: "Act as an adversarial reviewer: chain two what-ifs, look for contradictions with earlier answers, ask for minimal reproductions. No hints.",
    9: "Senior-reviewer scrutiny: require precise reasoning, invariants and test cases. Probe race conditions, resource exhaustion and security boundaries. Never accept hand-waving.",
    10: "Relentless principal-engineer scrutiny: chain multiple what-ifs, cross-examine against everything the student said before, demand invariants and failure analysis. No hints, no shortcuts.",
}

_TABLE = [
    # n, name, scrutiny, hist, recalls, ctx, max_tok, samples, temp, recall_p, words, bugs, effort
    (1, "Warm-up", "gentle", 6, 2, 2048, 500, 1, 0.70, 0.08, 120, 1, "low"),
    (2, "Explorer", "gentle", 8, 3, 3072, 550, 1, 0.65, 0.10, 130, 2, "low"),
    (3, "Apprentice", "light", 10, 3, 4096, 600, 1, 0.60, 0.12, 140, 2, "low"),
    (4, "Debugger", "moderate", 12, 4, 5120, 650, 1, 0.55, 0.14, 150, 2, "medium"),
    (5, "Analyst", "moderate", 14, 5, 6144, 700, 1, 0.50, 0.16, 160, 3, "medium"),
    (6, "Architect", "firm", 18, 6, 8192, 800, 2, 0.45, 0.18, 170, 3, "medium"),
    (7, "Skeptic", "strict", 22, 7, 10240, 900, 2, 0.40, 0.20, 180, 3, "medium"),
    (8, "Adversary", "strict", 26, 8, 12288, 1000, 2, 0.35, 0.22, 190, 4, "high"),
    (9, "Sentinel", "severe", 32, 10, 16384, 1100, 3, 0.30, 0.25, 200, 4, "high"),
    (10, "Grandmaster", "relentless", 40, 12, 24576, 1300, 3, 0.25, 0.30, 220, 5, "high"),
]

LEVELS: dict[int, Level] = {
    row[0]: Level(
        number=row[0],
        name=row[1],
        scrutiny=row[2],
        rules=_RULES[row[0]],
        history=row[3],
        recalls=row[4],
        context_tokens=row[5],
        max_tokens=row[6],
        samples=row[7],
        temperature=row[8],
        recall_probability=row[9],
        max_words=row[10],
        bugs_per_challenge=row[11],
        reasoning_effort=row[12],
    )
    for row in _TABLE
}


def get_level(number: int | None) -> Level:
    return LEVELS[max(1, min(10, int(number or 1)))]


def level_public(level: Level, budget_tokens: int) -> dict:
    effective = min(level.context_tokens, budget_tokens)
    return {
        "number": level.number,
        "name": level.name,
        "scrutiny": level.scrutiny,
        "rules": level.rules,
        "history_messages": level.history,
        "memory_recalls": level.recalls,
        "context_tokens_requested": level.context_tokens,
        "context_tokens_effective": effective,
        "context_limited": effective < level.context_tokens,
        "samples": level.samples,
        "reasoning_effort": level.reasoning_effort,
    }
