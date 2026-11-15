"""Turn uploaded files / pasted code into a Project and queue its analysis."""
from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath

from fastapi import HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import UPLOAD_DIR, get_settings
from ..models import Job, Project, User
from .ingest import IMAGE_EXTS, Ingested, build_content, ingest_upload

MAX_UPLOAD_FILES = 60


def guess_paste_name(code: str) -> str:
    sample = code[:4000]
    rules = [
        (r"^\s*(def |import |from \w+ import|class \w+\(?.*\)?:)", "pasted.py"),
        (r"#include\s*<", "pasted.cpp"),
        (r"\bpublic\s+(static\s+)?(class|void)\b", "pasted.java"),
        (r"\bfunc\s+\w+\(|package\s+main", "pasted.go"),
        (r"\bfn\s+\w+\(|let\s+mut\b", "pasted.rs"),
        (r"\b(const|let|var)\s+\w+\s*=|=>|function\s+\w+\(|console\.log", "pasted.js"),
        (r"<\?php", "pasted.php"),
        (r"^\s*(SELECT|INSERT|CREATE TABLE)\b", "pasted.sql"),
    ]
    for pattern, name in rules:
        if re.search(pattern, sample, re.MULTILINE | re.IGNORECASE):
            return name
    return "pasted.txt"


async def read_uploads(files: list[UploadFile], pasted_code: str = "") -> Ingested:
    max_bytes = get_settings().app.max_upload_mb * 1024 * 1024
    result = Ingested()
    total = 0
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(status_code=413, detail=f"Too many files (max {MAX_UPLOAD_FILES}); zip the project instead.")
    for upload in files:
        if not upload or not upload.filename:
            continue
        data = await upload.read(max_bytes + 1)
        total += len(data)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail=f"Upload too large (max {get_settings().app.max_upload_mb} MB).")
        # zip parsing + image header checks are CPU work: keep them off the event loop
        await asyncio.to_thread(ingest_upload, upload.filename, data, result)
    if pasted_code.strip():
        result.texts.append((guess_paste_name(pasted_code), pasted_code[:400_000]))
    return result


async def create_project(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    description: str,
    ingested: Ingested,
    source: str,
    enqueue: bool,
) -> tuple[Project | None, Job | None]:
    from ..workers.queue import queue

    if not ingested.texts and not ingested.images:
        return None, None
    files = [{"path": p, "bytes": len(t), "kind": "text"} for p, t in ingested.texts]
    for i, (path, data) in enumerate(ingested.images):
        ext = PurePosixPath(path.lower()).suffix
        files.append({"path": path, "stored": f"img_{i:02d}{ext if ext in IMAGE_EXTS else '.png'}", "bytes": len(data), "kind": "image"})
    first = ingested.texts[0][0] if ingested.texts else (ingested.images[0][0] if ingested.images else "project")
    project = Project(
        user_id=user.id,
        name=(name.strip() or PurePosixPath(first).stem or "My project")[:160],
        description=description.strip()[:4000],
        source=source,
        languages=ingested.languages,
        files=files,
        content=build_content(ingested.texts),
        analysis={"skipped": ingested.skipped[:50]},
        status="queued",
    )
    session.add(project)
    await session.flush()
    if ingested.images:
        folder = UPLOAD_DIR / str(project.id)
        folder.mkdir(parents=True, exist_ok=True)
        stored = [f for f in files if f["kind"] == "image"]
        for meta, (_, data) in zip(stored, ingested.images):
            (folder / meta["stored"]).write_bytes(data)
    job = None
    if enqueue:
        job = await queue.enqueue(session, kind="project_ingest", user_id=user.id, payload={"project_id": str(project.id)},
                                  label=f"Ingest '{project.name}' (OCR layers + pgvector)", priority=1)
    else:
        project.analysis = {**dict(project.analysis), "error": "Waiting for the AI servers - press 'Analyse' once they are online."}
    return project, job
