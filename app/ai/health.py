"""AI availability monitor.

Pings the AI servers every ``ai.health_interval_seconds`` (default 5 s). The AI gateway and
the Qwen coder are REQUIRED: the moment either stops responding every queued / running /
waiting job is halted and marked failed (via outage listeners) and the browser shows a
blocking "AI server stopped working" dialog.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ..config import blocked_model_reason, get_settings, looks_blocked
from ..db import db_ping
from .clients import primary
from .providers import http

log = logging.getLogger("cte.health")
REQUIRED_ROLES = ("gateway", "coder")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slots(value: Any) -> tuple[int, int] | None:
    """'1/2' (busy/total) -> (1, 2)."""
    if isinstance(value, str) and "/" in value:
        busy, _, total = value.partition("/")
        try:
            return max(0, int(busy)), max(0, int(total))
        except ValueError:
            return None
    return None


def recommend_concurrency(backends: list[dict[str, Any]], ours: int, *, cpu_percent: Any = None,
                          fallback: int = 4, ceiling: int = 16) -> tuple[int, dict[str, Any]]:
    """How many requests may this app run in parallel on the AI server right now?

    * PC free (no other work in its slots, CPU not pegged by something else): ALL slots -> maximum speed.
    * Other work running: leave the slots it uses; if the CPU is also busy, take half of what is left.
    The server's busy count includes our own requests, so they are not counted as "other work".
    """
    total = busy = 0
    for backend in backends:
        if not backend.get("online", True) or backend.get("overloaded"):
            continue
        parsed = _slots(backend.get("busy_slots"))
        if parsed is None:
            continue
        used, cap = parsed
        total += cap
        busy += min(used, cap)
    try:
        cpu = float(cpu_percent) if cpu_percent is not None else None
    except (TypeError, ValueError):
        cpu = None
    if total == 0:
        return max(1, min(ceiling, fallback)), {"mode": "fixed", "slots": 0, "others_busy": 0, "cpu_percent": cpu}
    others = max(0, busy - ours)
    cpu_busy = cpu is not None and cpu >= 85 and ours == 0  # something else keeps the PC busy
    if others == 0 and not cpu_busy:
        limit, mode = total, "max"
    else:
        free = max(1, total - others)
        limit, mode = (max(1, (free + 1) // 2) if cpu_busy else free), "shared"
    return max(1, min(ceiling, limit)), {"mode": mode, "slots": total, "others_busy": others, "cpu_percent": cpu}


class HealthMonitor:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.roles: dict[str, dict[str, Any]] = {
            role: {"ok": False, "detail": "not checked yet", "checked_at": None} for role in ("gateway", "coder", "vision")
        }
        self.database: dict[str, Any] = {"ok": False, "detail": "not checked yet"}
        self.ai_online = False
        self.checked_once = False
        self.outage_count = 0
        self.last_outage: dict[str, Any] | None = None
        self.last_recovery: dict[str, Any] | None = None
        self._listeners: list[Callable[[str], Awaitable[int]]] = []
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._recheck: asyncio.Task | None = None
        self._ai_server_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._ai_server_confirmed: dict[str, dict[str, Any] | None] = {}  # last conclusive /status answer per server

    # ------------------------------------------------------------------ public API
    def add_outage_listener(self, callback: Callable[[str], Awaitable[int]]) -> None:
        self._listeners.append(callback)

    def role_ok(self, role: str) -> bool:
        return bool(self.roles.get(role, {}).get("ok"))

    def vision_enabled(self) -> bool:
        flag = self.settings.ai.vision.enabled
        if isinstance(flag, bool):
            return flag
        return str(flag).strip().lower() not in {"false", "off", "no", "0", "disabled"}

    def outage_reason(self) -> str:
        down = [
            f"{self.roles[r].get('label') or r}: {self.roles[r].get('detail', 'offline')}"
            for r in REQUIRED_ROLES
            if not self.roles[r].get("ok")
        ]
        return "AI server stopped working - " + "; ".join(down) if down else ""

    def test_mode(self) -> bool:
        return any(str(getattr(self.settings.ai, r).model).startswith("mock-") for r in REQUIRED_ROLES)

    def snapshot(self) -> dict[str, Any]:
        return {
            "ai_online": self.ai_online,
            "checked_once": self.checked_once,
            "required_roles": list(REQUIRED_ROLES),
            "roles": self.roles,
            "database": self.database,
            "outage_count": self.outage_count,
            "last_outage": self.last_outage,
            "last_recovery": self.last_recovery,
            "reason": "" if self.ai_online else (self.outage_reason() if self.checked_once else "Checking AI servers..."),
            "interval_seconds": self.settings.ai.health_interval_seconds,
            "test_mode": self.test_mode(),
            "server_time": _now(),
        }

    # ------------------------------------------------------------------ checks
    async def ai_server_info(self, base_url: str, api_key: str) -> dict[str, Any] | None:
        """Detect the AI server gateway (its /status lists load-balanced backends). Cached for 30 s.

        Only a real answer counts: a slow, dropped or 5xx /status reply proves nothing, so the last confirmed
        answer is kept (before any answer the gateway is "unverified", not blocked). Otherwise one busy moment
        on the AI server would make its Gemma look "locally run", block it and halt every task."""
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        now = time.monotonic()
        cached = self._ai_server_cache.get(root)
        # fresh every health check when the gateway's slots drive our concurrency, else every 30 s
        ttl = max(2.0, self.settings.ai.health_interval_seconds - 1.0) if self.settings.ai.gateway.adaptive_concurrency else 30.0
        if cached and now - cached[0] < ttl:
            return cached[1]
        info: dict[str, Any] | None = None
        answered = False
        try:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            resp = await http().get(f"{root}/status", headers=headers, timeout=self.settings.ai.health_timeout_seconds)
            if resp.status_code == 200:
                answered = True
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and isinstance(data.get("backends"), list):
                    info = {
                        # names / load only - backend URLs (your network layout) never reach the browser
                        "backends": [{k: b.get(k) for k in ("name", "online", "busy_slots", "overloaded")}
                                     for b in data["backends"] if isinstance(b, dict)][:16],
                        "cpu_percent": data.get("this_pc_cpu_percent"),
                    }
            elif resp.status_code in (401, 403, 404, 405):  # it answered: there is no AI-server status page here
                answered = True
        except Exception:  # noqa: BLE001 - timeout / connection error: inconclusive
            answered = False
        if answered:
            self._ai_server_confirmed[root] = info
        else:
            info = self._ai_server_confirmed.get(root, {"unverified": True, "backends": [], "cpu_percent": None})
        self._ai_server_cache[root] = (now, info)
        return info

    async def _ping(self, role: str) -> dict[str, Any]:
        ep = getattr(self.settings.ai, role)
        parsed = urlparse(ep.base_url)
        info: dict[str, Any] = {
            "label": ep.label or role,
            "model": ep.model,
            "endpoint": f"{parsed.hostname}:{parsed.port}" if parsed.port else str(parsed.hostname),
            "checked_at": _now(),
        }
        ai_server = None
        if role == "gateway" or looks_blocked(self.settings, role):
            ai_server = await self.ai_server_info(ep.base_url, ep.api_key)
            if ai_server and not ai_server.get("unverified"):
                info["ai_server"] = ai_server
        blocked = blocked_model_reason(self.settings, role, via_ai_server=bool(ai_server))
        if blocked:
            return {**info, "ok": False, "reachable": None, "blocked": True, "detail": blocked}
        timeout = self.settings.ai.health_timeout_seconds
        result = await primary(role).ping(timeout)
        if not result["ok"]:  # one quick re-check to avoid flapping on a single dropped packet
            await asyncio.sleep(0.4)
            result = await primary(role).ping(timeout)
        if role == "gateway" and ep.adaptive_concurrency and ai_server and not ai_server.get("unverified"):
            info["concurrency"] = await self._adapt_gateway(ep, ai_server)
        return {**info, **result, "blocked": False}

    async def _adapt_gateway(self, ep: Any, ai_server: dict[str, Any]) -> dict[str, Any]:
        """Push the AI server to its maximum when it is free, step back when other work is using it."""
        provider = primary("gateway")
        ours = int(provider.concurrency["active"] or 0)
        limit, detail = recommend_concurrency(ai_server.get("backends") or [], ours, cpu_percent=ai_server.get("cpu_percent"),
                                              fallback=int(ep.max_concurrency or 4), ceiling=int(ep.concurrency_ceiling or 16))
        if provider.concurrency["limit"] != limit:
            await provider.set_concurrency(limit)
            log.info("gateway concurrency -> %d (%s)", limit, detail["mode"])
        return {**detail, "limit": limit, "ours": ours}

    def request_check(self) -> None:
        """Run an immediate health check (e.g. a worker just saw a connection failure)."""
        if self._recheck is None or self._recheck.done():
            self._recheck = asyncio.create_task(self.check_once(), name="cte-health-recheck")

    async def check_once(self) -> dict[str, Any]:
        async with self._lock:  # periodic and on-demand checks never overlap
            return await self._check_once()

    async def _check_once(self) -> dict[str, Any]:
        jobs: list[Awaitable[dict[str, Any]]] = [self._ping("gateway"), self._ping("coder"), db_ping()]
        vision = self.vision_enabled()
        if vision:
            jobs.append(self._ping("vision"))
        results = await asyncio.gather(*jobs, return_exceptions=True)

        def _safe(value: Any, role: str) -> dict[str, Any]:
            if isinstance(value, BaseException):
                return {"ok": False, "detail": f"health check error: {value.__class__.__name__}", "checked_at": _now(), "label": role}
            return value

        self.roles["gateway"] = _safe(results[0], "gateway")
        self.roles["coder"] = _safe(results[1], "coder")
        self.database = _safe(results[2], "database")
        if vision:
            self.roles["vision"] = _safe(results[3], "vision")
        else:
            self.roles["vision"] = {"ok": False, "disabled": True, "detail": "disabled in config", "checked_at": _now()}

        required_ok = all(self.roles[r].get("ok") for r in REQUIRED_ROLES)
        was_online = self.ai_online
        self.ai_online = required_ok
        if not required_ok and (was_online or not self.checked_once):
            reason = self.outage_reason()
            if was_online:
                self.outage_count += 1
                self.last_outage = {"at": _now(), "reason": reason, "halted_jobs": 0}
                log.warning("AI outage detected -> halting all work: %s", reason)
            halted = 0
            for callback in self._listeners:
                try:
                    halted += int(await callback(reason) or 0)
                except Exception:  # noqa: BLE001
                    log.exception("outage listener failed")
            if was_online and self.last_outage is not None:
                self.last_outage["halted_jobs"] = halted
        elif required_ok and not was_online and self.checked_once:
            self.last_recovery = {"at": _now()}
            log.info("AI servers are back online")
        self.checked_once = True
        return self.snapshot()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        interval = max(1.0, float(self.settings.ai.health_interval_seconds))
        while True:
            started = loop.time()
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("health check crashed")
            await asyncio.sleep(max(0.5, interval - (loop.time() - started)))

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="cte-health-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


monitor = HealthMonitor()
