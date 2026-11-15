"""Standalone worker process - adds more parallel AI workers without another web server.

    python -m app.worker

It drains the same PostgreSQL job queue (FOR UPDATE SKIP LOCKED), runs its own 5-second AI
health monitor (halting its in-flight jobs on an outage) and listens on Redis pub/sub for
instant wake-ups and halt broadcasts from the web process.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from .ai.clients import close_all
from .ai.health import monitor
from .ai.providers import close_http
from .bus import bus
from .db import engine, init_db
from .services.hardware import detect_hardware
from .workers import tasks  # noqa: F401 - registers job handlers
from .workers.queue import queue


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s worker %(name)s: %(message)s")
    await init_db()
    await asyncio.to_thread(detect_hardware)
    queue.attach_bus()
    await bus.start()
    monitor.add_outage_listener(queue.halt_all)
    await monitor.check_once()
    monitor.start()
    await queue.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass
    try:
        await stop.wait()
    finally:
        await queue.stop()
        await monitor.stop()
        await bus.stop()
        await close_all()
        await close_http()
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
