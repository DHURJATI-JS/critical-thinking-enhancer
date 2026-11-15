"""Text embeddings for pgvector memory.

Provider order: fastembed (local CPU ONNX, default) -> OpenAI-compatible endpoint -> a
deterministic hashing embedder that always works offline. Each stored vector records the
embedder name so similarity search only compares vectors from the same model.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import threading
from typing import Any

from ..config import RUNTIME_DIR, get_settings
from .providers import OpenAICompatProvider

log = logging.getLogger("cte.embeddings")


class HashingEmbedder:
    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.name = f"hashing-v1-{dim}"

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = re.findall(r"[a-z0-9_]+", text.lower())
        feats: list[str] = tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
        for tok in tokens:
            padded = f"#{tok}#"
            feats.extend(padded[i : i + 3] for i in range(len(padded) - 2))
        for feat in feats:
            h = int.from_bytes(hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest(), "little")
            vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class FastEmbedder:
    def __init__(self, model: str, dim: int) -> None:
        from fastembed import TextEmbedding  # heavy import, done lazily

        cache = RUNTIME_DIR / "models" / "fastembed"
        cache.mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(model_name=model, cache_dir=str(cache))
        self._lock = threading.Lock()
        self.name = f"fastembed:{model}"
        self.dim = dim

    def _run(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            return [[float(x) for x in vec] for vec in self._model.embed(texts)]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._run, texts)


class RemoteEmbedder:
    def __init__(self, base_url: str, api_key: str, model: str, dim: int) -> None:
        self._provider = OpenAICompatProvider("embeddings", base_url, api_key, model, timeout=60)
        self.name = f"remote:{model}"
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._provider.embed(texts, dim=self.dim)


_embedder: Any = None
_lock = asyncio.Lock()


async def get_embedder() -> Any:
    global _embedder
    if _embedder is not None:
        return _embedder
    async with _lock:
        if _embedder is not None:
            return _embedder
        cfg = get_settings().embeddings
        if cfg.provider == "fastembed":
            try:
                _embedder = await asyncio.to_thread(FastEmbedder, cfg.model, cfg.dim)
            except Exception as exc:  # noqa: BLE001 - offline / not installed -> hashing
                log.warning("fastembed unavailable (%s); using hashing embeddings", exc)
                _embedder = HashingEmbedder(cfg.dim)
        elif cfg.provider == "openai_compatible" and cfg.base_url:
            _embedder = RemoteEmbedder(cfg.base_url, cfg.api_key, cfg.model, cfg.dim)
        else:
            _embedder = HashingEmbedder(cfg.dim)
    return _embedder


async def embed_texts(texts: list[str]) -> tuple[str, list[list[float]]]:
    """Returns (embedder_name, vectors). Never raises: falls back to hashing."""
    dim = get_settings().embeddings.dim
    clipped = [(t or " ")[:2000] for t in texts]
    embedder = await get_embedder()
    try:
        vectors = await embedder.embed(clipped)
        if len(vectors) != len(clipped) or any(len(v) != dim for v in vectors):
            raise ValueError("embedding dimension mismatch")
        return embedder.name, vectors
    except Exception as exc:  # noqa: BLE001
        log.warning("embedding failed with %s (%s); using hashing", getattr(embedder, "name", "?"), exc)
        fallback = HashingEmbedder(dim)
        return fallback.name, await fallback.embed(clipped)


def active_embedder_name() -> str:
    return getattr(_embedder, "name", "not loaded yet")
