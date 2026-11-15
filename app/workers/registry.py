"""Job handler registry."""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class JobOutcome:
    result: dict[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    low_confidence: bool = False
    provider: str = ""


@dataclass
class JobContext:
    job_id: int
    kind: str
    user_id: uuid.UUID | None
    payload: dict[str, Any]
    label: str = ""


Handler = Callable[[JobContext], Awaitable["JobOutcome | dict[str, Any] | None"]]
FailureHook = Callable[[JobContext, str], Awaitable[None]]

HANDLERS: dict[str, Handler] = {}
FAILURE_HOOKS: dict[str, FailureHook] = {}


def job_handler(kind: str) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return decorator


def on_failure(kind: str) -> Callable[[FailureHook], FailureHook]:
    def decorator(fn: FailureHook) -> FailureHook:
        FAILURE_HOOKS[kind] = fn
        return fn

    return decorator
