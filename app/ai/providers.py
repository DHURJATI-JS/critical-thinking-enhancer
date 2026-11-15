"""AI provider clients.

* :class:`OpenAICompatProvider` uses the official OpenAI Python SDK (``AsyncOpenAI``) against any
  OpenAI-compatible server: the AI gateway, vLLM (Qwen coder / MiniCPM-V) and the OpenAI API (fallback).
  Health pings use a plain HTTP GET so outage detection never waits on SDK retries.
* :class:`AnthropicProvider` talks to the Anthropic Messages API (fallback).

Optional request parameters (``response_format``, ``logprobs``, ``reasoning_effort``,
``temperature``, ``max_tokens`` vs ``max_completion_tokens``) are learnt per server: if a
server rejects one with HTTP 400 the request is retried without it.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, BadRequestError, OpenAIError

_client: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            follow_redirects=False,
        )
    return _client


async def close_http() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


@dataclass
class ChatResult:
    text: str
    provider: str
    model: str
    latency_ms: int
    usage: dict = field(default_factory=dict)
    token_confidence: float | None = None


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, *, status: int | None = None, offline: bool = False):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.status = status
        self.offline = offline


_OFFLINE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.PoolTimeout)


class AdaptiveLimit:
    """A semaphore whose size can change while requests are in flight (adaptive gateway concurrency)."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self.active = 0
        self._cond = asyncio.Condition()

    async def __aenter__(self) -> "AdaptiveLimit":
        async with self._cond:
            await self._cond.wait_for(lambda: self.active < self.limit)
            self.active += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        async with self._cond:
            self.active -= 1
            self._cond.notify_all()

    async def set_limit(self, limit: int) -> None:
        async with self._cond:
            self.limit = max(1, int(limit))
            self._cond.notify_all()  # a raised limit lets waiting requests start right away


class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 120.0,
        supports_json_mode: bool = True,
        supports_logprobs: bool = False,
        send_reasoning_effort: bool = False,
        use_max_completion_tokens: bool = False,
        send_temperature: bool = True,
        token_multiplier: float = 1.0,
        max_concurrency: int | None = None,
        max_retries: int = 2,
    ) -> None:
        self._slots = AdaptiveLimit(max_concurrency) if max_concurrency else None
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # Official OpenAI SDK pointed at this server. base_url and api_key are ALWAYS explicit so a
        # global OPENAI_API_KEY / OPENAI_BASE_URL in the environment can never hijack or leak.
        # The SDK retries 429 / 5xx / connection errors with exponential backoff (honours Retry-After).
        self._client = AsyncOpenAI(base_url=self.base_url, api_key=api_key or "sk-no-key-required",
                                   timeout=timeout, max_retries=max_retries)
        self.supports_json_mode = supports_json_mode
        self.supports_logprobs = supports_logprobs
        self.send_reasoning_effort = send_reasoning_effort
        self.use_max_completion_tokens = use_max_completion_tokens
        self.send_temperature = send_temperature
        self.token_multiplier = token_multiplier

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"

    @property
    def concurrency(self) -> dict[str, int | None]:
        """Parallel-request limit and how many of this app's requests are running right now."""
        if self._slots is None:
            return {"limit": None, "active": 0}
        return {"limit": self._slots.limit, "active": self._slots.active}

    async def set_concurrency(self, limit: int) -> None:
        if self._slots is None:
            self._slots = AdaptiveLimit(limit)
        else:
            await self._slots.set_limit(limit)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _body(self, messages, temperature, max_tokens, json_mode, reasoning_effort, logprobs) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.send_temperature and temperature is not None:
            body["temperature"] = temperature
        tokens = int(max_tokens * self.token_multiplier)
        body["max_completion_tokens" if self.use_max_completion_tokens else "max_tokens"] = tokens
        if json_mode and self.supports_json_mode:
            body["response_format"] = {"type": "json_object"}
        if logprobs and self.supports_logprobs:
            body["logprobs"] = True
        if reasoning_effort and self.send_reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        return body

    def _adapt(self, error_text: str) -> bool:
        """Disable optional params the server rejected. Returns True if a retry makes sense."""
        t = error_text.lower()
        changed = False
        if ("response_format" in t or "json_object" in t) and self.supports_json_mode:
            self.supports_json_mode = False
            changed = True
        if "logprobs" in t and self.supports_logprobs:
            self.supports_logprobs = False
            changed = True
        if "reasoning_effort" in t and self.send_reasoning_effort:
            self.send_reasoning_effort = False
            changed = True
        if "max_tokens" in t and "max_completion_tokens" in t and not self.use_max_completion_tokens:
            self.use_max_completion_tokens = True
            changed = True
        if "temperature" in t and self.send_temperature:
            self.send_temperature = False
            changed = True
        return changed

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = 0.4,
        max_tokens: int = 800,
        json_mode: bool = False,
        reasoning_effort: str | None = None,
        logprobs: bool = True,
    ) -> ChatResult:
        if self._slots is None:
            return await self._chat(messages, temperature, max_tokens, json_mode, reasoning_effort, logprobs)
        async with self._slots:  # never send more parallel requests than the server has slots
            return await self._chat(messages, temperature, max_tokens, json_mode, reasoning_effort, logprobs)

    async def _chat(self, messages, temperature, max_tokens, json_mode, reasoning_effort, logprobs) -> ChatResult:
        """One chat completion through the official OpenAI SDK."""
        started = time.perf_counter()
        adapted = 0
        while True:
            kwargs = self._body(messages, temperature, max_tokens, json_mode, reasoning_effort, logprobs)
            try:
                resp = await self._client.chat.completions.create(**kwargs)
                break
            except APITimeoutError as exc:  # subclass of APIConnectionError - must be caught first
                raise ProviderError(self.name, f"request timed out after {self.timeout:.0f}s") from exc
            except APIConnectionError as exc:
                raise ProviderError(self.name, f"connection failed ({exc.__class__.__name__})", offline=True) from exc
            except BadRequestError as exc:
                if adapted < 3 and self._adapt(str(exc)):
                    adapted += 1
                    continue
                raise ProviderError(self.name, f"HTTP 400: {str(exc)[:300]}", status=400) from exc
            except APIStatusError as exc:  # after the SDK's own retries
                raise ProviderError(self.name, f"HTTP {exc.status_code}: {str(exc)[:300]}", status=exc.status_code,
                                    offline=exc.status_code in (502, 503, 504)) from exc
            except OpenAIError as exc:
                raise ProviderError(self.name, f"{exc.__class__.__name__}: {str(exc)[:300]}") from exc
        if not resp.choices:
            raise ProviderError(self.name, "malformed response: no choices")
        choice = resp.choices[0]
        content = (choice.message.content if choice.message else None) or ""
        token_conf = None
        if choice.logprobs is not None and choice.logprobs.content:
            probs = [math.exp(max(-20.0, float(t.logprob))) for t in choice.logprobs.content if t.logprob is not None]
            if probs:
                token_conf = sum(probs) / len(probs)
        return ChatResult(
            text=str(content).strip(),
            provider=self.name,
            model=str(resp.model or self.model),
            latency_ms=int((time.perf_counter() - started) * 1000),
            usage=resp.usage.model_dump() if resp.usage else {},
            token_confidence=token_conf,
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def ping(self, timeout: float) -> dict[str, Any]:
        started = time.perf_counter()
        base = {"ok": False, "reachable": False, "latency_ms": None, "models": [], "model_found": False, "max_model_len": None}
        try:
            resp = await http().get(f"{self.base_url}/models", headers=self._headers(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - any failure means unreachable
            return {**base, "detail": f"unreachable ({exc.__class__.__name__})"}
        latency = int((time.perf_counter() - started) * 1000)
        base.update(reachable=True, latency_ms=latency)
        if resp.status_code in (401, 403):
            return {**base, "detail": f"authentication failed (HTTP {resp.status_code}) - check the API key"}
        if resp.status_code >= 400:
            return {**base, "detail": f"HTTP {resp.status_code} from /models"}
        try:
            payload = resp.json()
        except ValueError:
            return {**base, "detail": "invalid JSON from /models"}
        models = payload.get("data") if isinstance(payload, dict) else None
        models = [m for m in (models or []) if isinstance(m, dict)]
        ids = [str(m.get("id")) for m in models if m.get("id")]
        found = (self.model in ids) if ids else True
        max_len = None
        for m in models:
            if m.get("id") == self.model:
                meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
                # vLLM: max_model_len - OpenRouter-style: context_length - llama.cpp: meta.n_ctx
                max_len = m.get("max_model_len") or m.get("context_length") or m.get("context_window") or meta.get("n_ctx")
        detail = "online" if found else f"model '{self.model}' is not served (available: {', '.join(ids[:5]) or 'none'})"
        return {**base, "ok": found, "models": ids[:20], "model_found": found, "max_model_len": max_len, "detail": detail}

    async def embed(self, texts: list[str], dim: int | None = None) -> list[list[float]]:
        kwargs: dict[str, Any] = {"model": self.model, "input": texts}
        if dim:
            kwargs["dimensions"] = dim
        try:
            resp = await self._client.embeddings.create(**kwargs)
        except APIConnectionError as exc:
            raise ProviderError(self.name, f"connection failed ({exc.__class__.__name__})", offline=True) from exc
        except APIStatusError as exc:
            raise ProviderError(self.name, f"embeddings HTTP {exc.status_code}: {str(exc)[:200]}", status=exc.status_code) from exc
        return [row.embedding for row in sorted(resp.data, key=lambda d: d.index)]


def _anthropic_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks: list[dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                media, data = url[5:].split(";base64,", 1)
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    return blocks or [{"type": "text", "text": ""}]


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.anthropic.com", timeout: float = 120.0):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.send_temperature = True

    @property
    def label(self) -> str:
        return f"anthropic:{self.model}"

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}

    @staticmethod
    def _convert(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if role == "system":
                system_parts.append(content if isinstance(content, str) else json.dumps(content))
                continue
            role = "assistant" if role == "assistant" else "user"
            blocks = _anthropic_blocks(content)
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})
        if not out or out[0]["role"] != "user":
            out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(conversation start)"}]})
        return "\n\n".join(system_parts), out

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = 0.4,
        max_tokens: int = 800,
        json_mode: bool = False,
        reasoning_effort: str | None = None,
        logprobs: bool = False,
    ) -> ChatResult:
        started = time.perf_counter()
        system, conv = self._convert(messages)
        if json_mode:
            system += "\n\nReturn ONLY one valid JSON object. No prose before or after it, no markdown fences."
        body: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "messages": conv}
        if system:
            body["system"] = system
        if self.send_temperature and temperature is not None:
            body["temperature"] = temperature
        resp: httpx.Response | None = None
        for attempt in range(2):
            try:
                resp = await http().post(f"{self.base_url}/v1/messages", json=body, headers=self._headers(), timeout=self.timeout)
            except _OFFLINE_ERRORS as exc:
                raise ProviderError(self.name, f"connection failed ({exc.__class__.__name__})", offline=True) from exc
            except httpx.TimeoutException as exc:
                raise ProviderError(self.name, "request timed out") from exc
            if resp.status_code == 400 and attempt == 0 and "temperature" in resp.text.lower() and "temperature" in body:
                body.pop("temperature", None)
                self.send_temperature = False
                continue
            break
        assert resp is not None
        if resp.status_code >= 400:
            raise ProviderError(self.name, f"HTTP {resp.status_code}: {resp.text[:300]}", status=resp.status_code)
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if isinstance(b, dict) and b.get("type") == "text")
        return ChatResult(
            text=text.strip(),
            provider=self.name,
            model=str(data.get("model") or self.model),
            latency_ms=int((time.perf_counter() - started) * 1000),
            usage=data.get("usage") or {},
        )

    async def ping(self, timeout: float) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            resp = await http().get(f"{self.base_url}/v1/models", headers=self._headers(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reachable": False, "latency_ms": None, "detail": f"unreachable ({exc.__class__.__name__})"}
        ok = resp.status_code == 200
        detail = "key valid" if ok else ("invalid API key" if resp.status_code in (401, 403) else f"HTTP {resp.status_code}")
        return {"ok": ok, "reachable": True, "latency_ms": int((time.perf_counter() - started) * 1000), "detail": detail}


def openai_fallback(api_key: str, model: str, base_url: str, timeout: float) -> OpenAICompatProvider:
    """Official OpenAI API (reasoning-model friendly defaults)."""
    return OpenAICompatProvider(
        "openai",
        base_url,
        api_key,
        model,
        timeout=timeout,
        supports_json_mode=True,
        supports_logprobs=False,
        send_reasoning_effort=True,
        use_max_completion_tokens=True,
        send_temperature=False,
        token_multiplier=3.0,  # reasoning tokens count against max_completion_tokens
    )
