"""AI spam protection.

Layer 1 - sliding-window rate limits per IP and per profile (+ temporary mutes), stored in
          Redis when available so limits hold across processes (in-memory fallback).
Layer 2 - heuristics: duplicates / flooding, gibberish, link spam, advertising words,
          prompt-injection attempts (which also protects the secret bug lists).
Layer 3 - messages in the grey zone are classified by the AI gateway (in parallel with
          the normal chat processing) and dropped if the AI is confident they are spam.
Plus a honeypot field and minimum fill time on the onboarding form.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque

from fastapi import HTTPException

from ..bus import bus
from ..config import get_settings

INJECTION_PATTERNS = [
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|prompts?|rules)",
    r"(?:reveal|show|print|leak|tell me) (?:me )?(?:the |your )?(?:system prompt|hidden (?:bugs?|answers?)|planted bugs?|secret|answer key|rubric|solution)",
    r"you are now (?:dan|in developer mode|unrestricted)",
    r"\bjailbreak\b",
    r"disregard (?:your|the|all) (?:rules|guidelines|instructions)",
    r"pretend (?:you have|there are) no (?:rules|restrictions)",
]
SPAM_WORDS = re.compile(
    r"(?i)\b(?:buy now|discount|casino|crypto giveaway|free money|click here|subscribe now|viagra|earn \$\d+|promo code|limited offer)\b"
)
CODE_HINT = re.compile(r"[{};()=<>\[\]]|\b(?:def|class|return|import|function|const|let|var|public|static|void|print)\b")


def _shingles(text: str, n: int = 3) -> set[str]:
    words = re.findall(r"\w+", text.lower())
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def looks_like_code(text: str) -> bool:
    return len(CODE_HINT.findall(text)) >= max(3, len(text) // 80)


def heuristic_score(text: str, recent: list[str]) -> tuple[float, list[str], bool]:
    """Returns (score 0..1, reasons, prompt_injection_suspected)."""
    t = (text or "").strip()
    if not t:
        return 1.0, ["empty message"], False
    score = 0.0
    reasons: list[str] = []
    injection = False

    sh = _shingles(t)
    for prev in recent[-6:]:
        if prev.strip() == t and len(t) > 3:
            score += 0.35
            reasons.append("exact repeat of a recent message")
            break
        if len(t) > 30 and _jaccard(sh, _shingles(prev)) > 0.9:
            score += 0.45
            reasons.append("near-duplicate of a recent message")
            break

    if len(t) >= 20 and not looks_like_code(t):
        alpha = [c for c in t.lower() if c.isalpha()]
        ascii_letters = [c for c in alpha if "a" <= c <= "z"]
        # Vowel statistics only make sense for Latin-script text - never flag Hindi, Chinese, Arabic ...
        if len(ascii_letters) >= 15 and len(ascii_letters) >= 0.6 * len(alpha):
            vowel_ratio = sum(c in "aeiouy" for c in ascii_letters) / len(ascii_letters)
            if len(ascii_letters) >= 25 and (vowel_ratio < 0.08 or vowel_ratio > 0.85):
                score += 0.85
                reasons.append("gibberish (keyboard mash)")
            elif vowel_ratio < 0.15 or vowel_ratio > 0.72:
                score += 0.4
                reasons.append("gibberish-like letter distribution")
        runs = [len(m.group(0)) for m in re.finditer(r"(.)\1+", t)]
        longest = max(runs, default=1)
        if longest >= 20:
            score += 0.85
            reasons.append("character flooding")
        elif longest >= 12:
            score += 0.4
            reasons.append("repeated characters")
        words = t.split()
        if len(words) >= 8 and len({w.lower() for w in words}) / len(words) < 0.25:
            score += 0.5
            reasons.append("the same words repeated")
        mash = [w for w in re.findall(r"[A-Za-z]{10,}", t) if not re.search(r"[aeiouyAEIOUY]", w)]
        if mash:
            score += 0.5
            reasons.append("keyboard-mash tokens")

    links = len(re.findall(r"https?://", t))
    if links >= 6:
        score += 0.7
        reasons.append(f"{links} links")
    elif links >= 3:
        score += 0.4
        reasons.append(f"{links} links")
    spam_hits = {m.group(0).lower() for m in SPAM_WORDS.finditer(t)}
    if len(spam_hits) >= 2:
        score += 0.7
        reasons.append("advertising / spam phrases")
    elif spam_hits:
        score += 0.35
        reasons.append("advertising phrase")

    if any(re.search(p, t, re.IGNORECASE) for p in INJECTION_PATTERNS):
        injection = True
        score += 0.3
        reasons.append("prompt-injection pattern")
    return min(1.0, score), reasons, injection


class SpamGuard:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._blocks: dict[str, deque[float]] = defaultdict(deque)
        self._muted_until: dict[str, float] = {}

    def _window(self, key: str, limit: int, window: float) -> float:
        """Returns 0 if allowed, else seconds until the next slot."""
        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= limit:
            return max(1.0, window - (now - hits[0]))
        hits.append(now)
        return 0.0

    async def check_rate(self, key: str, kind: str, multiplier: int = 1) -> None:
        """Redis-backed (shared across processes) with an in-memory fallback.

        ``multiplier`` widens the allowance for shared keys (an IP address can hide a whole
        classroom behind NAT, so per-IP limits are looser than per-profile limits).
        """
        cfg = get_settings().spam
        muted = await bus.muted_for(key)
        if muted is None:
            muted = self._muted_until.get(key, 0.0) - time.monotonic()
        if muted > 0:
            raise HTTPException(status_code=429, detail=f"Spam protection: you are muted for {int(muted) + 1}s.")
        limits = {
            "chat": (cfg.chat_per_minute, 60.0),
            "upload": (cfg.upload_per_minute, 60.0),
            "onboard": (cfg.onboard_per_10min, 600.0),
            "challenge": (max(4, cfg.chat_per_minute // 2), 60.0),
            "keys": (6, 60.0),
            "tts": (120, 60.0),
            "tts_anon": (20, 60.0),  # signed-out onboarding avatar
            "level": (12, 60.0),     # manual scrutiny changes
            "login": (10, 600.0),  # brute-force protection: 10 tries / 10 min per username (x4 per IP)
        }
        limit, window = limits.get(kind, (30, 60.0))
        limit *= max(1, multiplier)
        wait = await bus.rate_limit(f"{kind}:{key}", limit, window)
        if wait is None:
            wait = self._window(f"{kind}:{key}", limit, window)
        if wait:
            raise HTTPException(status_code=429, detail=f"Spam protection: too many requests, retry in {int(wait) + 1}s.")

    async def record_block(self, key: str) -> bool:
        """Remember a blocked message; mutes the sender after repeated blocks. Returns True if muted."""
        cfg = get_settings().spam
        shared = await bus.record_block(key, cfg.mute_after_blocks, 600, cfg.mute_seconds)
        if shared is not None:
            return shared
        now = time.monotonic()
        blocks = self._blocks[key]
        blocks.append(now)
        while blocks and blocks[0] <= now - 600:
            blocks.popleft()
        if len(blocks) >= cfg.mute_after_blocks:
            self._muted_until[key] = now + cfg.mute_seconds
            blocks.clear()
            return True
        return False


guard = SpamGuard()
