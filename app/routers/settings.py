"""User settings: theme, avatar face, voice, AI fallback (own Claude / OpenAI key, encrypted at rest)."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.providers import AnthropicProvider, openai_fallback
from ..config import get_settings
from ..db import get_session
from ..models import User, json_merge, utcnow
from ..security.auth import client_ip, current_user
from ..security.crypto import decrypt, encrypt, mask
from ..security.spam import guard

router = APIRouter(prefix="/api/settings", tags=["settings"])
FACES = {"bot", "ada", "neo", "kit"}


def keys_status(user: User) -> dict[str, Any]:
    out = {}
    for provider in ("anthropic", "openai"):
        enc = (user.secrets or {}).get(f"{provider}_key")
        plain = decrypt(enc) if enc else None
        status = (user.secrets or {}).get(f"{provider}_status") or {}
        out[provider] = {"set": bool(plain), "masked": mask(plain) if plain else None, **status}
    return out


@router.get("")
async def get_user_settings(user: User = Depends(current_user)) -> dict[str, Any]:
    cfg = get_settings().ai.fallback
    return {"settings": dict(user.settings or {}), "keys": keys_status(user),
            "fallback_models": {"anthropic": cfg.anthropic_model, "openai": cfg.openai_model}}


class AvatarIn(BaseModel):
    face: str = "bot"
    hue: int = Field(default=190, ge=0, le=360)
    name: str = Field(default="Byte", min_length=1, max_length=40)


class VoiceIn(BaseModel):
    enabled: bool = True
    engine: Literal["neural", "browser"] = "neural"
    neural: str = Field(default="", max_length=60)
    name: str = Field(default="", max_length=200)
    rate: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch: float = Field(default=1.0, ge=0.0, le=2.0)
    volume: float = Field(default=1.0, ge=0.0, le=1.0)


class FallbackIn(BaseModel):
    provider: Literal["none", "anthropic", "openai"] = "none"
    enabled: bool = True
    for_long_context: bool = False
    model: str = Field(default="", max_length=80)


class SettingsIn(BaseModel):
    theme: Literal["dark", "light", "system"] | None = None
    avatar: AvatarIn | None = None
    voice: VoiceIn | None = None
    fallback: FallbackIn | None = None


@router.put("")
async def update_settings(body: SettingsIn, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if body.theme is not None:
        user.settings["theme"] = body.theme
    if body.avatar is not None:
        avatar = body.avatar.model_dump()
        avatar["face"] = avatar["face"] if avatar["face"] in FACES else "bot"
        avatar["name"] = avatar["name"].strip()[:40] or "Byte"
        json_merge(user.settings, "avatar", avatar)
    if body.voice is not None:
        json_merge(user.settings, "voice", body.voice.model_dump())
    if body.fallback is not None:
        json_merge(user.settings, "fallback", body.fallback.model_dump())
    await session.commit()
    return {"settings": dict(user.settings), "keys": keys_status(user)}


class KeyIn(BaseModel):
    provider: Literal["anthropic", "openai"]
    key: str = Field(min_length=10, max_length=400)


@router.put("/keys")
async def save_key(body: KeyIn, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    await guard.check_rate(f"ip:{client_ip(request)}", "keys")
    key = body.key.strip()
    cfg = get_settings().ai.fallback
    model = ((user.settings or {}).get("fallback") or {}).get("model") or (cfg.anthropic_model if body.provider == "anthropic" else cfg.openai_model)
    provider = (AnthropicProvider(key, model, cfg.anthropic_base_url, cfg.timeout_seconds) if body.provider == "anthropic"
                else openai_fallback(key, model, cfg.openai_base_url, cfg.timeout_seconds))
    result = await provider.ping(10)
    if not result.get("reachable"):
        raise HTTPException(status_code=502, detail=f"Could not reach {body.provider}: {result.get('detail')}")
    detail = str(result.get("detail", ""))
    rejected = not result.get("ok") and any(word in detail.lower() for word in ("auth", "invalid", "401", "403"))
    if rejected:
        raise HTTPException(status_code=400, detail=f"The {body.provider} key was rejected: {detail}")
    user.secrets[f"{body.provider}_key"] = encrypt(key)
    user.secrets[f"{body.provider}_status"] = {"valid": True, "checked_at": utcnow().isoformat(),
                                               "detail": result.get("detail"), "model": model}
    fallback = dict((user.settings or {}).get("fallback") or {})
    if fallback.get("provider") in (None, "none"):
        fallback["provider"] = body.provider
        fallback.setdefault("enabled", True)
        user.settings["fallback"] = fallback
    await session.commit()
    return {"keys": keys_status(user), "settings": dict(user.settings)}


@router.delete("/keys/{provider}")
async def delete_key(provider: Literal["anthropic", "openai"], user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    user.secrets.pop(f"{provider}_key", None)
    user.secrets.pop(f"{provider}_status", None)
    fallback = dict((user.settings or {}).get("fallback") or {})
    if fallback.get("provider") == provider:
        fallback["provider"] = "none"
        user.settings["fallback"] = fallback
    await session.commit()
    return {"keys": keys_status(user), "settings": dict(user.settings)}
