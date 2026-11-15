"""PostgreSQL-backed job queue with concurrent async workers.

* ``enqueue`` / ``fan_out`` insert rows; workers claim them with
  ``UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED)`` so several workers (or several
  processes) can drain the queue in parallel without double-processing.
* ``fan_out`` creates a parent "group" row plus N children that run in parallel; when the last
  child finishes an aggregator job is enqueued automatically.
* ``halt_all`` (called by the health monitor on an AI outage) cancels every in-flight task and
  marks all queued / running / waiting jobs as failed.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.health import monitor
from ..ai.providers import ProviderError
from ..ai.router import AIUnavailable
from ..bus import EVENTS_CHANNEL, INSTANCE_ID, JOBS_CHANNEL, bus
from ..config import UPLOAD_DIR, get_settings
from ..db import SessionLocal
from ..models import Job, Project
from .registry import FAILURE_HOOKS, HANDLERS, JobContext, JobOutcome

log = logging.getLogger("cte.queue")

HEARTBEAT_SECONDS = 15  # running jobs are touched this often (database clock)...
STALE_SECONDS = 90      # ...so one silent for this long belongs to a dead process
LABEL_MAX = 160         # jobs.label column width
RESTART_REASON = "Interrupted (the server restarted) - please retry."
CRASH_REASON = "Interrupted (the server restarted or a worker crashed) - please retry."

_CLAIM_SQL = text(
    """
    UPDATE jobs SET status = 'running', started_at = now(), heartbeat_at = now(), attempts = attempts + 1
    WHERE id = (
        SELECT id FROM jobs WHERE status = 'queued'
        ORDER BY priority DESC, id ASC
        LIMIT 1 FOR UPDATE SKIP LOCKED
    )
    RETURNING id
    """
)
# "still being worked on" includes a project's analysis group and the aggregation it will start (their payload
# carries the project id inside on_complete) - otherwise a project with a pending aggregation looked abandoned
_ACTIVE = ("SELECT 1 FROM jobs j WHERE j.status IN ('queued','running','waiting') AND "
           "(j.payload->>'{key}' = {table}.id::text OR j.payload->'on_complete'->'payload'->>'{key}' = {table}.id::text)")


class JobQueue:
    def __init__(self) -> None:
        cfg = get_settings().workers
        self.concurrency = max(1, cfg.concurrency)
        self.timeout = float(cfg.job_timeout_seconds)
        self._wake = asyncio.Event()
        self._workers: list[asyncio.Task] = []
        self._inflight: dict[int, asyncio.Task] = {}
        self._stopping = False
        self.processed = 0
        self.failed = 0
        self.halted = 0

    # ------------------------------------------------------------------ producer side
    async def enqueue(
        self,
        session: AsyncSession,
        *,
        kind: str,
        user_id: uuid.UUID | None,
        payload: dict[str, Any],
        label: str = "",
        priority: int = 0,
        parent_id: int | None = None,
    ) -> Job:
        job = Job(kind=kind, user_id=user_id, payload=payload, label=(label or kind)[:LABEL_MAX], priority=priority,
                  parent_id=parent_id, status="queued")
        session.add(job)
        await session.flush()
        return job

    async def fan_out(
        self,
        session: AsyncSession,
        *,
        group_kind: str,
        user_id: uuid.UUID | None,
        label: str,
        children: list[dict[str, Any]],
        on_complete: dict[str, Any],
        priority: int = 0,
    ) -> Job:
        parent = Job(
            kind=group_kind,
            user_id=user_id,
            label=label[:LABEL_MAX],
            status="waiting",
            pending_children=len(children),
            payload={"on_complete": on_complete},
            started_at=func.now(),  # database clock, like every other job timestamp the janitor compares
            priority=priority,
        )
        session.add(parent)
        await session.flush()
        for child in children:
            session.add(
                Job(
                    kind=child["kind"],
                    user_id=user_id,
                    payload=child.get("payload", {}),
                    label=str(child.get("label", child["kind"]))[:LABEL_MAX],
                    priority=priority,
                    parent_id=parent.id,
                    status="queued",
                )
            )
        await session.flush()
        return parent

    def wake(self) -> None:
        self._wake.set()

    def cancel_jobs(self, job_ids: list[int]) -> int:
        """Stop jobs running in this process (e.g. their account was just deleted). Returns how many."""
        cancelled = 0
        for job_id in job_ids:
            task = self._inflight.get(job_id)
            if task is not None and not task.done():
                task.cancel()
                cancelled += 1
        return cancelled

    async def notify(self) -> None:
        """Wake local workers and - through Redis pub/sub - workers in other processes."""
        self.wake()
        await bus.publish(JOBS_CHANNEL, {"type": "wake"})

    def attach_bus(self) -> None:
        async def on_jobs(_: dict[str, Any]) -> None:
            self.wake()

        async def on_events(message: dict[str, Any]) -> None:
            if message.get("type") == "halt" and message.get("origin") != INSTANCE_ID:
                ids = list(self._inflight)
                for task in list(self._inflight.values()):
                    task.cancel()
                if ids:  # the originator normally failed these rows already - make sure none is left "running"
                    reason = str(message.get("reason") or "AI server stopped working")[:400]
                    async with SessionLocal() as session:
                        await session.execute(update(Job).where(Job.id.in_(ids), Job.status == "running")
                                              .values(status="failed", error=reason, finished_at=func.now()))
                        await session.commit()

        bus.subscribe(JOBS_CHANNEL, on_jobs)
        bus.subscribe(EVENTS_CHANNEL, on_events)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._stopping = False
        for i in range(self.concurrency):
            self._workers.append(asyncio.create_task(self._worker(i), name=f"cte-worker-{i}"))
        self._workers.append(asyncio.create_task(self.janitor(), name="cte-queue-janitor"))
        self._workers.append(asyncio.create_task(self._heartbeat(), name="cte-queue-heartbeat"))
        log.info("job queue started with %d parallel workers", self.concurrency)

    async def stop(self) -> None:
        self._stopping = True
        inflight = list(self._inflight)
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        if not inflight:
            return
        # graceful restart: hand this process's unfinished jobs back at once instead of waiting for the janitor
        try:
            async with SessionLocal() as session:
                rows = (await session.execute(
                    update(Job).where(Job.id.in_(inflight), Job.status == "running")
                    .values(status="failed", error=RESTART_REASON, finished_at=func.now()).returning(Job.id, Job.parent_id)
                )).all()
                for _, parent_id in rows:
                    if parent_id:
                        await self._child_finished(session, parent_id)
                await self._release_domains(session, RESTART_REASON)
                await session.commit()
        except Exception:  # noqa: BLE001 - shutting down anyway; the janitor of the next run recovers them
            log.exception("could not release in-flight jobs on shutdown")

    async def _heartbeat(self) -> None:
        """Touch every job this process is running, so other processes' janitors know it is alive."""
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            ids = list(self._inflight)
            if not ids:
                continue
            try:
                async with SessionLocal() as session:
                    await session.execute(update(Job).where(Job.id.in_(ids), Job.status == "running").values(heartbeat_at=func.now()))
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - DB hiccup: the next beat retries
                log.warning("job heartbeat failed", exc_info=True)

    async def _idle(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    async def _claim(self) -> int | None:
        async with SessionLocal() as session:
            row = (await session.execute(_CLAIM_SQL)).first()
            await session.commit()
            return int(row[0]) if row else None

    async def _worker(self, index: int) -> None:
        while not self._stopping:
            try:
                job_id = await self._claim()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - DB hiccup: back off and retry
                log.exception("worker %d failed to claim a job", index)
                await asyncio.sleep(2)
                continue
            if job_id is None:
                await self._idle(1.0)
                continue
            task = asyncio.create_task(self._execute(job_id), name=f"cte-job-{job_id}")
            self._inflight[job_id] = task
            try:
                await task
            except asyncio.CancelledError:
                if self._stopping:
                    raise
                # halted by halt_all(): the DB row is already marked failed
            except Exception:  # noqa: BLE001
                log.exception("job %s crashed", job_id)
            finally:
                self._inflight.pop(job_id, None)

    # ------------------------------------------------------------------ execution
    async def _execute(self, job_id: int) -> None:
        async with SessionLocal() as session:
            job = await session.get(Job, job_id)
            if job is None or job.status != "running":
                return
            ctx = JobContext(job_id=job.id, kind=job.kind, user_id=job.user_id, payload=dict(job.payload or {}), label=job.label)
        handler = HANDLERS.get(ctx.kind)
        if handler is None:
            await self._finish(ctx, "failed", error=f"no handler registered for job kind '{ctx.kind}'")
            return
        try:
            out = await asyncio.wait_for(handler(ctx), timeout=self.timeout)
            outcome = out if isinstance(out, JobOutcome) else JobOutcome(result=dict(out or {}))
            await self._finish(ctx, "succeeded", outcome=outcome)
        except asyncio.CancelledError:
            raise
        except AIUnavailable as exc:
            await self._finish(ctx, "failed", error=str(exc))
        except asyncio.TimeoutError:
            await self._finish(ctx, "failed", error=f"timed out after {self.timeout:.0f}s")
        except ProviderError as exc:
            if exc.offline:  # connection-level failure: re-check health now instead of waiting for the next ping
                monitor.request_check()
                await self._finish(ctx, "failed", error=f"AI server stopped working - {exc}")
            else:
                await self._finish(ctx, "failed", error=f"AI provider error: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s (%s) failed", ctx.job_id, ctx.kind)
            await self._finish(ctx, "failed", error=f"{exc.__class__.__name__}: {exc}"[:500])

    async def _finish(self, ctx: JobContext, status: str, *, outcome: JobOutcome | None = None, error: str = "") -> None:
        outcome = outcome or JobOutcome()
        enqueued = False
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    update(Job)
                    .where(Job.id == ctx.job_id, Job.status == "running")
                    .values(
                        status=status,
                        result=outcome.result,
                        error=error,
                        confidence=outcome.confidence,
                        low_confidence=outcome.low_confidence,
                        provider=outcome.provider,
                        finished_at=func.now(),
                    )
                    .returning(Job.parent_id)
                )
            ).first()
            if row is None:  # halted meanwhile - keep the "failed" verdict from halt_all
                await session.commit()
                return
            parent_id = row[0]
            if parent_id:
                enqueued = await self._child_finished(session, parent_id)
            group_id = ctx.payload.get("group_id")
            if group_id:
                await session.execute(
                    update(Job)
                    .where(Job.id == int(group_id), Job.status == "running")
                    .values(status=status, error=error, finished_at=func.now(), result={"aggregate_job": ctx.job_id})
                )
            await session.commit()
        if status == "succeeded":
            self.processed += 1
        else:
            self.failed += 1
            hook = FAILURE_HOOKS.get(ctx.kind)
            if hook is not None:
                try:
                    await hook(ctx, error)
                except Exception:  # noqa: BLE001
                    log.exception("failure hook for %s crashed", ctx.kind)
        if enqueued:
            await self.notify()

    async def _child_finished(self, session: AsyncSession, parent_id: int) -> bool:
        remaining = (
            await session.execute(
                update(Job)
                .where(Job.id == parent_id, Job.status == "waiting")
                .values(pending_children=Job.pending_children - 1)
                .returning(Job.pending_children)
            )
        ).scalar_one_or_none()
        if remaining is None or remaining > 0:
            return False
        parent = await session.get(Job, parent_id)
        if parent is None:
            return False
        on_complete = dict((parent.payload or {}).get("on_complete") or {})
        if not on_complete:
            parent.status = "succeeded"
            parent.finished_at = func.now()
            return False
        parent.status = "running"
        session.add(
            Job(
                kind=on_complete["kind"],
                user_id=parent.user_id,
                payload={**on_complete.get("payload", {}), "group_id": parent_id},
                label=str(on_complete.get("label", on_complete["kind"]))[:LABEL_MAX],
                priority=int(on_complete.get("priority", parent.priority or 0)),
                status="queued",
            )
        )
        return True

    # ------------------------------------------------------------------ outage handling
    async def halt_all(self, reason: str) -> int:
        """Cancel everything in flight and mark all unfinished jobs failed."""
        async with SessionLocal() as session:
            result = await session.execute(
                update(Job)
                .where(Job.status.in_(("queued", "running", "waiting")))
                .values(status="failed", error=reason, finished_at=func.now())
                .returning(Job.id)
            )
            ids = [r[0] for r in result]
            params = {"reason": reason}
            await session.execute(
                text("UPDATE projects SET status = 'failed', analysis = analysis || jsonb_build_object('error', CAST(:reason AS text)) "
                     "WHERE status IN ('queued', 'analyzing')"),
                params,
            )
            await session.execute(
                text("UPDATE challenges SET status = 'failed', meta = meta || jsonb_build_object('error', CAST(:reason AS text)) "
                     "WHERE status = 'generating'"),
                params,
            )
            await session.execute(
                text("UPDATE challenges SET status = 'open', meta = meta || jsonb_build_object('error', CAST(:reason AS text)) "
                     "WHERE status = 'evaluating'"),
                params,
            )
            await session.commit()
        for task in list(self._inflight.values()):
            task.cancel()
        await bus.publish(EVENTS_CHANNEL, {"type": "halt", "reason": reason})
        self.halted += len(ids)
        if ids:
            log.warning("halted %d job(s): %s", len(ids), reason)
        return len(ids)

    async def _release_domains(self, session: AsyncSession, reason: str) -> None:
        """Projects / challenges stuck mid-analysis with no job left working on them become retryable."""
        await session.execute(text(
            "UPDATE projects SET status = 'failed', analysis = analysis || jsonb_build_object('error', CAST(:reason AS text)) "
            f"WHERE status IN ('queued', 'analyzing') AND NOT EXISTS ({_ACTIVE.format(key='project_id', table='projects')})"
        ), {"reason": "Analysis was interrupted - press Analyse to retry."})
        await session.execute(text(
            "UPDATE challenges SET status = 'failed', meta = meta || jsonb_build_object('error', CAST(:reason AS text)) "
            f"WHERE status = 'generating' AND NOT EXISTS ({_ACTIVE.format(key='challenge_id', table='challenges')})"
        ), {"reason": reason})
        await session.execute(text(
            "UPDATE challenges SET status = 'open', meta = meta || jsonb_build_object('error', CAST(:reason AS text)) "
            f"WHERE status = 'evaluating' AND NOT EXISTS ({_ACTIVE.format(key='challenge_id', table='challenges')})"
        ), {"reason": reason})

    async def recover_stale(self, *, startup: bool = False) -> int:
        """Fail jobs whose worker process died - no heartbeat for STALE_SECONDS on the DATABASE clock (never the
        host clock, which drifts against Docker's after sleep). Jobs still heart-beating in another live worker
        process are never touched, not even at startup. Recovered children still let their project group finish."""
        notify = False
        async with SessionLocal() as session:
            rows = (await session.execute(text(
                "UPDATE jobs SET status = 'failed', error = :reason, finished_at = now() "
                "WHERE status = 'running' AND kind <> 'project_group' "
                f"AND COALESCE(heartbeat_at, started_at, created_at) < now() - interval '{STALE_SECONDS} seconds' "
                "RETURNING id, kind, user_id, parent_id, payload"
            ), {"reason": CRASH_REASON})).all()
            for row in rows:
                if row.parent_id:  # one analysis died: the group goes on (the aggregator reports what succeeded)
                    notify = await self._child_finished(session, row.parent_id) or notify
                group_id = (row.payload or {}).get("group_id")
                if group_id:  # the aggregator itself died: its group is over
                    await session.execute(update(Job).where(Job.id == int(group_id), Job.status == "running")
                                          .values(status="failed", error=CRASH_REASON, finished_at=func.now()))
            # safety net: a group with no child and no aggregator left can never complete
            await session.execute(text(
                "UPDATE jobs g SET status = 'failed', error = :reason, finished_at = now() "
                "WHERE g.kind = 'project_group' AND g.status IN ('waiting', 'running') "
                f"AND COALESCE(g.started_at, g.created_at) < now() - interval '{STALE_SECONDS} seconds' "
                "AND NOT EXISTS (SELECT 1 FROM jobs c WHERE c.status IN ('queued', 'running', 'waiting') "
                "AND (c.parent_id = g.id OR c.payload->>'group_id' = g.id::text))"
            ), {"reason": CRASH_REASON})
            await self._release_domains(session, CRASH_REASON)
            await session.commit()
        for row in rows:  # the clean-up the job would have done itself (reopen a challenge, delete a temp image, ...)
            hook = FAILURE_HOOKS.get(row.kind)
            if hook is not None:
                ctx = JobContext(job_id=row.id, kind=row.kind, user_id=row.user_id, payload=dict(row.payload or {}), label="")
                try:
                    await hook(ctx, CRASH_REASON)
                except Exception:  # noqa: BLE001
                    log.exception("failure hook for %s crashed", row.kind)
        if notify:
            await self.notify()
        if rows:
            log.warning("recovered %d stale job(s)", len(rows))
        return len(rows)

    async def sweep_files(self) -> int:
        """Delete upload leftovers older than an hour: chat OCR images whose job never ran (halted / crashed) and
        screenshot folders of projects that no longer exist."""
        cutoff = time.time() - 3600

        def candidates() -> tuple[list[Any], list[Any]]:
            if not UPLOAD_DIR.exists():
                return [], []
            files, folders = [], []
            for entry in UPLOAD_DIR.iterdir():
                try:
                    if entry.stat().st_mtime > cutoff:
                        continue
                except OSError:
                    continue
                if entry.is_file() and entry.name.startswith("ocr_"):
                    files.append(entry)
                elif entry.is_dir():
                    folders.append(entry)
            return files, folders

        files, folders = await asyncio.to_thread(candidates)
        removed = 0
        active = set()
        async with SessionLocal() as session:
            active = {str(p) for p in (await session.execute(
                select(Job.payload["path"].astext).where(Job.kind == "ocr_image", Job.status.in_(("queued", "running")))
            )).scalars() if p}
            ids = []
            for folder in folders:
                try:
                    ids.append(uuid.UUID(folder.name))
                except ValueError:
                    continue
            existing = {str(pid) for pid in (await session.execute(select(Project.id).where(Project.id.in_(ids)))).scalars()} if ids else set()
        for path in files:
            if str(path) not in active:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        for folder in folders:
            try:
                uuid.UUID(folder.name)
            except ValueError:
                continue
            if folder.name not in existing:
                await asyncio.to_thread(shutil.rmtree, folder, True)
                removed += 1
        if removed:
            log.info("removed %d upload leftover(s)", removed)
        return removed

    async def janitor(self) -> None:
        sweeps = 0
        while True:
            await asyncio.sleep(60)
            try:
                await self.recover_stale()
                sweeps += 1
                if sweeps % 10 == 1:  # every ~10 minutes
                    await self.sweep_files()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("janitor sweep failed")

    async def stats(self, user_id: uuid.UUID | None = None) -> dict[str, Any]:
        """Aggregate counts for everyone; the recent-jobs list only for the asking user (labels contain
        project / challenge titles, which are private to their owner)."""
        async with SessionLocal() as session:
            rows = (await session.execute(select(Job.status, func.count()).group_by(Job.status))).all()
            now = (await session.execute(select(func.now()))).scalar_one()
            recent = []
            if user_id is not None:
                recent = (await session.execute(select(Job).where(Job.user_id == user_id).order_by(Job.id.desc()).limit(12))).scalars().all()
        return {
            "workers": self.concurrency,
            "in_flight": len(self._inflight),
            "by_status": {status: count for status, count in rows},
            "processed_this_run": self.processed,
            "failed_this_run": self.failed,
            "halted_this_run": self.halted,
            "backend": "PostgreSQL (FOR UPDATE SKIP LOCKED)",
            "redis": bus.status(),
            "recent": [
                {"id": j.id, "kind": j.kind, "label": j.label, "status": j.status, "confidence": j.confidence,
                 "low_confidence": j.low_confidence, "provider": j.provider, "error": j.error[:160],
                 "created_at": j.created_at.isoformat() if j.created_at else None, **job_timing(j, now)}
                for j in recent
            ],
        }


def job_timing(job: Job, now: Any) -> dict[str, Any]:
    """How long a job waited / has been running / took (seconds, all on the database clock)."""
    def secs(a: Any, b: Any) -> float | None:
        return round(max(0.0, (a - b).total_seconds()), 1) if a is not None and b is not None else None

    out: dict[str, Any] = {"started_at": job.started_at.isoformat() if job.started_at else None,
                           "finished_at": job.finished_at.isoformat() if job.finished_at else None}
    if job.status in ("succeeded", "failed"):
        out["duration_seconds"] = secs(job.finished_at, job.started_at or job.created_at)
    elif job.status in ("running", "waiting") and job.started_at is not None:
        out["running_seconds"] = secs(now, job.started_at)
    elif job.status == "queued":
        out["waiting_seconds"] = secs(now, job.created_at)
    return out


queue = JobQueue()
