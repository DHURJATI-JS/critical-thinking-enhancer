"""Local neural text-to-speech with Piper (ONNX). Speech is generated on this machine - nothing
leaves it. The browser plays the WAV through a Web Audio analyser and drives the avatar's lip sync
from the real loudness of the voice.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import threading
import urllib.request
import wave
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..config import RUNTIME_DIR

log = logging.getLogger("cte.tts")

HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
VOICE_DIR = RUNTIME_DIR / "models" / "piper"
VOICES: dict[str, dict[str, str]] = {
    "en_US-amy-medium": {"label": "Amy - US English, female", "path": "en/en_US/amy/medium"},
    "en_US-ryan-medium": {"label": "Ryan - US English, male", "path": "en/en_US/ryan/medium"},
    "en_US-lessac-medium": {"label": "Lessac - US English, female", "path": "en/en_US/lessac/medium"},
    "en_US-hfc_female-medium": {"label": "HFC - US English, female", "path": "en/en_US/hfc_female/medium"},
    "en_US-hfc_male-medium": {"label": "HFC - US English, male", "path": "en/en_US/hfc_male/medium"},
    "en_GB-alan-medium": {"label": "Alan - British English, male", "path": "en/en_GB/alan/medium"},
    "en_GB-jenny_dioco-medium": {"label": "Jenny - British English, female", "path": "en/en_GB/jenny_dioco/medium"},
}
DEFAULT_VOICE = "en_US-amy-medium"
MAX_CHARS = 1200
CACHE_ENTRIES = 96
CACHE_BYTES = 64 * 1024 * 1024
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def speech_text(markdown: str) -> str:
    """Markdown -> what should be said out loud (code blocks are not read character by character)."""
    t = str(markdown or "")
    t = re.sub(r"```[\s\S]*?(```|$)", " (the code is on your screen) ", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", " a link ", t)
    t = _EMOJI.sub(" ", t)
    t = re.sub(r"[*_#>|~]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:MAX_CHARS]


class PiperTTS:
    def __init__(self) -> None:
        self._voices: dict[str, Any] = {}
        self._load_lock = threading.Lock()
        self._synth_locks: dict[str, threading.Lock] = {}
        self._downloads: dict[str, threading.Thread] = {}
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_bytes = 0
        self._cache_lock = threading.Lock()
        self.last_error: str | None = None

    # -------------------------------------------------------------- installation
    @property
    def available(self) -> bool:
        try:
            import piper  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"piper-tts not installed: {exc}"
            return False
        return True

    @staticmethod
    def files(voice_id: str) -> tuple[Path, Path]:
        return VOICE_DIR / f"{voice_id}.onnx", VOICE_DIR / f"{voice_id}.onnx.json"

    def installed(self, voice_id: str) -> bool:
        model, config = self.files(voice_id)
        return model.exists() and config.exists() and model.stat().st_size > 1_000_000

    def download(self, voice_id: str) -> None:
        """Blocking download of one voice (~60 MB) from the official rhasspy/piper-voices repo."""
        if voice_id not in VOICES:
            raise ValueError(f"unknown voice {voice_id}")
        if self.installed(voice_id):
            return
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        for suffix in (".onnx.json", ".onnx"):
            url = f"{HF_BASE}/{VOICES[voice_id]['path']}/{voice_id}{suffix}"
            dest = VOICE_DIR / f"{voice_id}{suffix}"
            tmp = dest.with_name(dest.name + ".part")
            request = urllib.request.Request(url, headers={"User-Agent": "cte-tts/1.0"})
            with urllib.request.urlopen(request, timeout=120) as response, open(tmp, "wb") as fh:  # noqa: S310 - fixed https URL
                shutil.copyfileobj(response, fh, 1 << 20)
            tmp.replace(dest)
        log.info("Piper voice %s downloaded", voice_id)

    def start_download(self, voice_id: str) -> None:
        thread = self._downloads.get(voice_id)
        if thread is not None and thread.is_alive():
            return

        def run() -> None:
            try:
                self.download(voice_id)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"download of {voice_id} failed: {exc}"
                log.warning(self.last_error)

        thread = threading.Thread(target=run, name=f"piper-download-{voice_id}", daemon=True)
        self._downloads[voice_id] = thread
        thread.start()

    def downloading(self, voice_id: str) -> bool:
        thread = self._downloads.get(voice_id)
        return bool(thread and thread.is_alive())

    # -------------------------------------------------------------- synthesis
    def _voice(self, voice_id: str) -> Any:
        voice = self._voices.get(voice_id)
        if voice is not None:
            return voice
        from piper import PiperVoice

        with self._load_lock:
            voice = self._voices.get(voice_id)
            if voice is None:
                model, config = self.files(voice_id)
                voice = PiperVoice.load(str(model), config_path=str(config))
                self._voices[voice_id] = voice
        return voice

    def synthesize(self, text: str, voice_id: str = DEFAULT_VOICE, rate: float = 1.0) -> bytes:
        """Blocking: returns a WAV file. Call through asyncio.to_thread."""
        from piper import SynthesisConfig

        voice_id = voice_id if voice_id in VOICES else DEFAULT_VOICE
        rate = max(0.5, min(2.0, float(rate or 1.0)))
        key = hashlib.sha256(f"{voice_id}|{rate:.2f}|{text}".encode("utf-8")).hexdigest()
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        voice = self._voice(voice_id)
        buffer = io.BytesIO()
        lock = self._synth_locks.setdefault(voice_id, threading.Lock())
        with lock, wave.open(buffer, "wb") as wav:
            voice.synthesize_wav(text, wav, syn_config=SynthesisConfig(length_scale=1.0 / rate))
        data = buffer.getvalue()
        with self._cache_lock:
            self._cache[key] = data
            self._cache_bytes += len(data)
            # bounded by entries AND bytes (a slow 1200-character sentence is ~6 MB of WAV)
            while self._cache and (len(self._cache) > CACHE_ENTRIES or self._cache_bytes > CACHE_BYTES):
                _, old = self._cache.popitem(last=False)
                self._cache_bytes -= len(old)
        return data

    def clear_cache(self) -> None:
        """Forget all synthesized speech (it can contain a deleted person's chat text)."""
        with self._cache_lock:
            self._cache.clear()
            self._cache_bytes = 0

    def warm(self, voice_id: str = DEFAULT_VOICE) -> None:
        """Load the default voice at startup so the first sentence is instant."""
        if self.available and self.installed(voice_id):
            try:
                self.synthesize("Ready.", voice_id)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)

    def status(self) -> dict[str, Any]:
        return {
            "engine": "piper",
            "available": self.available,
            "default": DEFAULT_VOICE,
            "voices": [{"id": vid, "label": v["label"], "installed": self.installed(vid), "downloading": self.downloading(vid)}
                       for vid, v in VOICES.items()],
            "error": self.last_error,
        }


tts = PiperTTS()
