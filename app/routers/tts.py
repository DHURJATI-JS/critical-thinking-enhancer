"""Text-to-speech endpoints (local Piper neural voices).

Synthesis is CPU work, so it runs on its own small thread pool (never the shared one that password checks,
embeddings and OCR use) with a short queue: when it is full the browser simply falls back to its own voice.
Signed-out visitors (the onboarding avatar) may only use already-installed voices, short texts, and only while
nobody else is using the app.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import User
from ..security.auth import client_ip, optional_user
from ..security.spam import guard
from ..services import session_lock
from ..services.tts import DEFAULT_VOICE, VOICES, speech_text, tts

router = APIRouter(prefix="/api/tts", tags=["tts"])
TTS_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cte-tts")
MAX_PENDING = 6
ANON_MAX_CHARS = 400
_pending = 0


@router.get("/voices")
async def voices() -> dict[str, Any]:
    return tts.status()


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    voice: str = Field(default="", max_length=60)
    rate: float = Field(default=1.0, ge=0.5, le=2.0)


@router.post("")
async def speak(body: SpeakIn, request: Request, user: User | None = Depends(optional_user),
                session: AsyncSession = Depends(get_session)) -> Response:
    global _pending
    if user is not None:
        # the person using the app (heartbeat-style: speaking does not count as activity, never takes the app)
        ok, other = await session_lock.claim(session, user, interaction=False)
        await session.commit()
        if not ok:
            raise HTTPException(status_code=423, detail={"code": "APP_IN_USE", "holder": session_lock.public_view(other).get("name"),
                                                         "message": "Someone else is using the app right now."})
        await guard.check_rate(str(user.id), "tts")
    else:
        # signed-out: the onboarding avatar only - and only while nobody else is using the app
        if await session_lock.holder(session) is not None:
            raise HTTPException(status_code=423, detail={"code": "APP_IN_USE", "message": "Someone else is using the app right now."})
        await guard.check_rate(f"ip:{client_ip(request)}", "tts_anon")
    text = speech_text(body.text)
    if user is None:
        text = text[:ANON_MAX_CHARS]
    if not text:
        raise HTTPException(status_code=400, detail="Nothing to say.")
    if not tts.available:
        raise HTTPException(status_code=503, detail={"code": "TTS_UNAVAILABLE", "message": tts.last_error or "Piper TTS is not installed."})
    voice = body.voice if body.voice in VOICES else DEFAULT_VOICE
    if not tts.installed(voice):
        if user is not None:
            tts.start_download(voice)  # never block a request on a 60 MB download; only for signed-in people
        raise HTTPException(status_code=503, detail={"code": "TTS_DOWNLOADING", "message": "This voice is being downloaded (about 60 MB)."
                                                     if user is not None else "This voice downloads after you create your profile."})
    if _pending >= MAX_PENDING:
        raise HTTPException(status_code=429, detail={"code": "TTS_BUSY", "message": "The voice is busy - using the browser voice for now."})
    _pending += 1
    try:
        wav = await asyncio.get_running_loop().run_in_executor(TTS_POOL, tts.synthesize, text, voice, body.rate)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail={"code": "TTS_FAILED", "message": f"{exc.__class__.__name__}: {exc}"[:200]}) from exc
    finally:
        _pending -= 1
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "private, max-age=86400"})
