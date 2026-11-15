"""Level progression state machine (stored in ``User.progression`` MutableDict).

* The progress bar fills as the session progresses (weighted by answer quality).
* At 100 % the AI ASKS whether the student is comfortable raising the difficulty.
* Declining keeps the level; after ``max_declines_before_lock`` declines the AI stops and
  states that the level is cleared and an upgrade is required to continue.
* Completing level 10 triggers a congratulation.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from ..config import get_settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_progression(start_level: int) -> dict[str, Any]:
    level = max(1, min(10, int(start_level or 1)))
    return {
        "level": level,
        "started_level": level,
        "progress": 0.0,
        "declines": 0,
        "offer_pending": False,
        "locked": False,
        "completed": False,
        "turns": 0,
        "turns_since_offer": 0,
        "history": [{"level": level, "event": "start", "at": _now()}],
    }


def normalize(prog: dict[str, Any] | None) -> dict[str, Any]:
    base = default_progression(int((prog or {}).get("level") or 1))
    base.update(copy.deepcopy(prog or {}))
    return base


def add_progress(prog: dict[str, Any], gain: float) -> tuple[dict[str, Any], list[str]]:
    p = normalize(prog)
    events: list[str] = []
    if p["locked"] or gain <= 0:
        return p, events
    p["progress"] = round(min(100.0, float(p["progress"]) + float(gain)), 2)
    p["turns"] += 1
    p["turns_since_offer"] += 1
    if p["progress"] >= 100.0:
        if p["level"] >= 10:
            if not p["completed"]:
                p["completed"] = True
                p["history"].append({"level": 10, "event": "completed", "at": _now()})
                events.append("completed")
        elif not p["offer_pending"]:
            between = get_settings().levels.turns_between_offers
            if p["declines"] == 0 or p["turns_since_offer"] >= between:
                p["offer_pending"] = True
                p["turns_since_offer"] = 0
                events.append("offer")
    return p, events


def _level_up(p: dict[str, Any], event: str) -> dict[str, Any]:
    p["level"] = min(10, int(p["level"]) + 1)
    p["progress"] = 0.0
    p["declines"] = 0
    p["offer_pending"] = False
    p["locked"] = False
    p["turns_since_offer"] = 0
    p["history"].append({"level": p["level"], "event": event, "at": _now()})
    return p


def respond_to_offer(prog: dict[str, Any], accept: bool) -> tuple[dict[str, Any], str]:
    """Returns (new_state, event) where event is level_up | declined | locked | no_offer."""
    p = normalize(prog)
    if not p["offer_pending"]:
        return p, "no_offer"
    if accept:
        return _level_up(p, "accepted_raise"), "level_up"
    p["offer_pending"] = False
    p["declines"] += 1
    p["turns_since_offer"] = 0
    p["history"].append({"level": p["level"], "event": "declined_raise", "at": _now()})
    if p["declines"] >= get_settings().levels.max_declines_before_lock:
        p["locked"] = True
        p["history"].append({"level": p["level"], "event": "level_cleared_locked", "at": _now()})
        return p, "locked"
    return p, "declined"


def set_level(prog: dict[str, Any], level: int) -> tuple[dict[str, Any], str]:
    """The student picks a scrutiny level themselves (Settings / chat). Returns (state, raised|lowered|same).
    The level bar restarts at 0 % and any pending raise offer or 'cleared' lock is resolved by their choice;
    a Level 10 completion stays earned."""
    p = normalize(prog)
    new = max(1, min(10, int(level)))
    old = int(p["level"])
    if new == old:
        return p, "same"
    p["level"] = new
    p["progress"] = 0.0
    p["declines"] = 0
    p["offer_pending"] = False
    p["locked"] = False
    p["turns_since_offer"] = 0
    p["history"].append({"level": new, "from": old, "event": "changed_by_student", "at": _now()})
    p["history"] = p["history"][-50:]
    return p, "raised" if new > old else "lowered"


def upgrade(prog: dict[str, Any]) -> tuple[dict[str, Any], str]:
    p = normalize(prog)
    if p["level"] >= 10:
        return p, "max_level"
    if not (p["locked"] or p["offer_pending"] or p["progress"] >= 100.0):
        return p, "not_ready"
    return _level_up(p, "upgraded"), "level_up"
