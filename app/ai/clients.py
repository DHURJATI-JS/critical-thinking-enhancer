"""Primary (configured) AI server clients, one per role."""
from __future__ import annotations

from ..config import get_settings
from .providers import OpenAICompatProvider

ROLES = ("gateway", "coder", "vision")
_PRIMARY: dict[str, OpenAICompatProvider] = {}


def primary(role: str) -> OpenAICompatProvider:
    provider = _PRIMARY.get(role)
    if provider is None:
        ep = getattr(get_settings().ai, role)
        provider = OpenAICompatProvider(
            role,
            ep.base_url,
            ep.api_key,
            ep.model,
            timeout=ep.timeout_seconds,
            supports_json_mode=ep.supports_json_mode,
            supports_logprobs=ep.supports_logprobs,
            send_reasoning_effort=ep.send_reasoning_effort,
            max_concurrency=ep.max_concurrency,
            max_retries=ep.max_retries,
        )
        _PRIMARY[role] = provider
    return provider


async def close_all() -> None:
    for provider in list(_PRIMARY.values()):
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001 - shutting down anyway
            pass
    _PRIMARY.clear()
