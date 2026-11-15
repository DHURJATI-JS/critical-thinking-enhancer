"""Route AI calls: primary server for the role, or the user's own Claude / OpenAI key.

The user's fallback key is used when (a) they enabled it and the local AI is offline, or
(b) they enabled "long context" mode and the current level needs more context memory than
this device can provide.
"""
from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from typing import Any

from ..config import get_settings
from ..models import User
from ..security.crypto import decrypt
from ..services.hardware import context_budget
from .clients import primary
from .health import monitor
from .providers import AnthropicProvider, ChatResult, OpenAICompatProvider, ProviderError, openai_fallback


class AIUnavailable(Exception):
    """Raised when no AI route is available (local AI offline and no usable fallback)."""


def fallback_settings(user: User | None) -> dict[str, Any]:
    if user is None:
        return {}
    return dict((user.settings or {}).get("fallback") or {})


# One provider object per (user, provider, model, key): keeps its HTTP connection pool and the parameters it
# learned (e.g. "this model rejects reasoning_effort") instead of rebuilding a client on every call.
_FALLBACKS: OrderedDict[tuple[str, ...], AnthropicProvider | OpenAICompatProvider] = OrderedDict()
_MAX_FALLBACKS = 8


def _evict(provider: Any) -> None:
    close = getattr(provider, "aclose", None)
    if close is None:
        return
    try:
        asyncio.get_running_loop().create_task(close())
    except RuntimeError:  # no running loop (tests / shutdown)
        pass


def user_fallback(user: User | None) -> AnthropicProvider | OpenAICompatProvider | None:
    prefs = fallback_settings(user)
    provider = prefs.get("provider")
    if user is None or provider not in ("anthropic", "openai"):
        return None
    token = (user.secrets or {}).get(f"{provider}_key")
    if not token:
        return None
    cfg = get_settings().ai.fallback
    model = prefs.get("model") or (cfg.anthropic_model if provider == "anthropic" else cfg.openai_model)
    cache_key = (str(user.id), provider, model, hashlib.sha256(str(token).encode()).hexdigest())
    cached = _FALLBACKS.get(cache_key)
    if cached is not None:
        _FALLBACKS.move_to_end(cache_key)
        return cached
    key = decrypt(token)
    if not key:
        return None
    if provider == "anthropic":
        built: AnthropicProvider | OpenAICompatProvider = AnthropicProvider(key, model, cfg.anthropic_base_url, cfg.timeout_seconds)
    else:
        built = openai_fallback(key, model, cfg.openai_base_url, cfg.timeout_seconds)
    _FALLBACKS[cache_key] = built
    while len(_FALLBACKS) > _MAX_FALLBACKS:
        _evict(_FALLBACKS.popitem(last=False)[1])
    return built


def fallback_ready(user: User | None) -> bool:
    """Cheap check (no client is built): has this person enabled a fallback and saved its key?"""
    prefs = fallback_settings(user)
    provider = prefs.get("provider")
    if user is None or provider not in ("anthropic", "openai") or not bool(prefs.get("enabled", True)):
        return False
    return bool((user.secrets or {}).get(f"{provider}_key"))


def can_run_now(user: User | None, roles: tuple[str, ...] = ("gateway", "coder")) -> tuple[bool, str]:
    if all(monitor.role_ok(r) for r in roles):
        return True, "primary"
    if fallback_ready(user):
        return True, "fallback"
    return False, monitor.outage_reason() or "AI server stopped working."


def role_budget(role: str) -> int:
    return context_budget(role, monitor.roles.get(role, {}).get("max_model_len"))


async def chat(
    role: str,
    messages: list[dict[str, Any]],
    *,
    user: User | None = None,
    need_tokens: int = 0,
    **kwargs: Any,
) -> ChatResult:
    fb = user_fallback(user)
    prefs = fallback_settings(user)
    if fb is not None and prefs.get("for_long_context") and need_tokens > role_budget(role):
        return await fb.chat(messages, **kwargs)
    if monitor.role_ok(role):
        try:
            return await primary(role).chat(messages, **kwargs)
        except ProviderError as exc:
            if exc.offline and fb is not None and prefs.get("enabled", True):
                return await fb.chat(messages, **kwargs)
            raise
    if fb is not None and prefs.get("enabled", True):
        return await fb.chat(messages, **kwargs)
    raise AIUnavailable(f"AI server stopped working: the {role} model is offline. {monitor.outage_reason()}".strip())
