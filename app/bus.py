"""Optional Redis bus.

Used for what Redis is good at:
  * distributed sliding-window rate limits, block counters and mutes (AI spam protection
    stays consistent across processes / worker instances),
  * pub/sub: instant worker wake-ups when jobs are enqueued and "halt" broadcasts so every
    worker process cancels its in-flight AI tasks the moment an outage is detected.

The durable job queue itself lives in PostgreSQL. If Redis is disabled or unreachable,
every call reports "unavailable" and callers fall back to in-process equivalents; a
reconnect is attempted every few seconds.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from .config import DEPLOYMENT_ID, deployment_namespace, get_settings  # noqa: F401 - deployment_namespace re-exported

log = logging.getLogger("cte.redis")

# Redis pub/sub channels are global to the Redis SERVER (the /0, /1 database number does not separate them), so they
# are scoped to this deployment's PostgreSQL database: another deployment on the same Redis - e.g. the isolated test
# stack - can never halt or wake this one, while processes sharing the database (app + extra workers) still talk.
JOBS_CHANNEL = f"cte:{DEPLOYMENT_ID}:jobs"
EVENTS_CHANNEL = f"cte:{DEPLOYMENT_ID}:events"
INSTANCE_ID = uuid.uuid4().hex[:12]

_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  return {0, tostring(oldest[2])}
end
redis.call('ZADD', key, now, ARGV[4])
redis.call('PEXPIRE', key, math.ceil(window * 1000))
return {1, '0'}
"""


class RedisBus:
    def __init__(self) -> None:
        self.cfg = get_settings().redis
        self.client: Any = None
        self.available = False
        self.detail = "not connected yet"
        self._last_attempt = 0.0
        self._script: Any = None
        self._listener: asyncio.Task | None = None
        self._handlers: dict[str, list[Callable[[dict[str, Any]], Awaitable[None]]]] = {}

    # ------------------------------------------------------------------ connection
    async def connect(self) -> bool:
        if not self.cfg.enabled:
            self.detail = "disabled in config.json"
            return False
        if self.available and self.client is not None:
            return True
        now = time.monotonic()
        if now - self._last_attempt < 5:
            return False
        self._last_attempt = now
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(
                self.cfg.url, decode_responses=True, socket_connect_timeout=1.5, socket_timeout=2.5, health_check_interval=15
            )
            await client.ping()
            self.client = client
            self._script = client.register_script(_SLIDING_WINDOW_LUA)
            self.available = True
            self.detail = "connected"
            log.info("Redis connected (%s)", self._redacted_url())
            return True
        except Exception as exc:  # noqa: BLE001
            self.available = False
            self.detail = f"unavailable ({exc.__class__.__name__}) - using in-memory fallback"
            return False

    def _down(self, exc: BaseException) -> None:
        self.available = False
        self.detail = f"connection lost ({exc.__class__.__name__}) - using in-memory fallback"

    def _redacted_url(self) -> str:
        parsed = urlparse(self.cfg.url)
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}{parsed.path}"

    # ------------------------------------------------------------------ rate limiting
    async def rate_limit(self, key: str, limit: int, window: float) -> float | None:
        """None = Redis unusable (caller falls back); 0 = allowed; >0 = seconds to wait."""
        if not await self.connect():
            return None
        try:
            now = time.time()
            allowed, oldest = await self._script(keys=[f"cte:rl:{key}"], args=[now, window, limit, f"{now}:{uuid.uuid4().hex[:8]}"])
            return 0.0 if int(allowed) else max(1.0, window - (now - float(oldest)))
        except Exception as exc:  # noqa: BLE001
            self._down(exc)
            return None

    async def muted_for(self, key: str) -> float | None:
        if not await self.connect():
            return None
        try:
            ttl = await self.client.pttl(f"cte:mute:{key}")
            return ttl / 1000.0 if ttl and ttl > 0 else 0.0
        except Exception as exc:  # noqa: BLE001
            self._down(exc)
            return None

    async def record_block(self, key: str, threshold: int, window: int, mute_seconds: int) -> bool | None:
        if not await self.connect():
            return None
        try:
            counter = f"cte:blocks:{key}"
            pipe = self.client.pipeline()
            pipe.incr(counter)
            pipe.expire(counter, int(window))
            count, _ = await pipe.execute()
            if int(count) >= threshold:
                await self.client.set(f"cte:mute:{key}", "1", ex=int(mute_seconds))
                await self.client.delete(counter)
                return True
            return False
        except Exception as exc:  # noqa: BLE001
            self._down(exc)
            return None

    # ------------------------------------------------------------------ pub/sub
    async def publish(self, channel: str, message: dict[str, Any]) -> bool:
        if not await self.connect():
            return False
        try:
            await self.client.publish(channel, json.dumps({**message, "origin": INSTANCE_ID}))
            return True
        except Exception as exc:  # noqa: BLE001
            self._down(exc)
            return False

    def subscribe(self, channel: str, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._handlers.setdefault(channel, []).append(handler)

    async def _listen(self) -> None:
        while True:
            if not await self.connect():
                await asyncio.sleep(5)
                continue
            pubsub = self.client.pubsub()
            try:
                await pubsub.subscribe(*self._handlers.keys())
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        data = json.loads(msg["data"])
                    except (TypeError, ValueError):
                        continue
                    for handler in self._handlers.get(msg.get("channel", ""), []):
                        try:
                            await handler(data)
                        except Exception:  # noqa: BLE001
                            log.exception("pub/sub handler failed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._down(exc)
                await asyncio.sleep(3)
            finally:
                try:
                    await pubsub.aclose()
                except Exception:  # noqa: BLE001
                    pass

    async def start(self) -> None:
        await self.connect()
        if self._handlers and self.cfg.enabled and (self._listener is None or self._listener.done()):
            self._listener = asyncio.create_task(self._listen(), name="cte-redis-listener")

    async def stop(self) -> None:
        if self._listener:
            self._listener.cancel()
            await asyncio.gather(self._listener, return_exceptions=True)
            self._listener = None
        if self.client is not None:
            try:
                await self.client.aclose()
            except Exception:  # noqa: BLE001
                pass
        self.client = None
        self.available = False

    def status(self) -> dict[str, Any]:
        return {"enabled": self.cfg.enabled, "available": self.available, "detail": self.detail, "url": self._redacted_url(),
                "instance": INSTANCE_ID}


bus = RedisBus()
