"""Safe project ingestion: single files, pasted code, .zip archives (zip-slip / bomb safe), images."""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

LANG_BY_EXT = {
    ".py": "python", ".ipynb": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript", ".java": "java", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp", ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".php": "php", ".swift": "swift", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".sql": "sql",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss", ".vue": "vue", ".svelte": "svelte",
    ".dart": "dart", ".lua": "lua", ".r": "r", ".m": "matlab", ".sh": "bash", ".bash": "bash", ".ps1": "powershell",
    ".bat": "batch", ".md": "markdown", ".txt": "text", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".xml": "xml", ".gradle": "gradle",
}
SPECIAL_NAMES = {"dockerfile": "docker", "makefile": "make", "cmakelists.txt": "cmake"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build", ".next", ".idea", ".vscode",
    "target", "bin", "obj", ".mypy_cache", ".pytest_cache", ".gradle", "coverage", ".tox", "site-packages",
}
MAX_TEXT_FILE_BYTES = 400_000
MAX_IMAGE_BYTES = 8_000_000
MAX_FILES = 400
MAX_ZIP_TOTAL = 60_000_000
MAX_ZIP_ENTRIES = 5000
MAX_COMPRESSION_RATIO = 200
MAX_IMAGES = 10
MAX_CONTENT_CHARS = 1_500_000


@dataclass
class Ingested:
    texts: list[tuple[str, str]] = field(default_factory=list)  # (path, text)
    images: list[tuple[str, bytes]] = field(default_factory=list)  # (path, bytes)
    skipped: list[str] = field(default_factory=list)

    @property
    def languages(self) -> list[str]:
        langs = {language_for(p) for p, _ in self.texts}
        return sorted(lang for lang in langs if lang not in ("text", "markdown", "json", "yaml", "ini", "toml"))


def language_for(path: str) -> str:
    name = PurePosixPath(path).name.lower()
    if name in SPECIAL_NAMES:
        return SPECIAL_NAMES[name]
    return LANG_BY_EXT.get(PurePosixPath(name).suffix, "text")


def _decode(data: bytes) -> str | None:
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    for encoding in ("utf-8-sig", "utf-16") if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _notebook_to_text(raw: str) -> str:
    try:
        nb = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    cells = []
    for cell in nb.get("cells", []):
        src = "".join(cell.get("source", [])) if isinstance(cell.get("source"), list) else str(cell.get("source", ""))
        if cell.get("cell_type") == "code":
            cells.append(src)
        elif cell.get("cell_type") == "markdown":
            cells.append("# " + src.replace("\n", "\n# "))
    return "\n\n".join(cells)


def _accept(result: Ingested, path: str, data: bytes) -> None:
    suffix = PurePosixPath(path.lower()).suffix
    if suffix in IMAGE_EXTS:
        from ..ocr.pipeline import image_problem

        if len(result.images) >= MAX_IMAGES or len(data) > MAX_IMAGE_BYTES:
            result.skipped.append(f"{path} (image limit)")
        elif (problem := image_problem(data)) is not None:  # format allow-list + decompression-bomb guard
            result.skipped.append(f"{path} ({problem})")
        else:
            result.images.append((path, data))
        return
    known = suffix in LANG_BY_EXT or PurePosixPath(path).name.lower() in SPECIAL_NAMES
    if not known:
        result.skipped.append(f"{path} (unsupported type)")
        return
    if len(data) > MAX_TEXT_FILE_BYTES:
        result.skipped.append(f"{path} (file too large)")
        return
    text = _decode(data)
    if text is None:
        result.skipped.append(f"{path} (binary)")
        return
    if suffix == ".ipynb":
        text = _notebook_to_text(text)
    if len(result.texts) >= MAX_FILES:
        result.skipped.append(f"{path} (file count limit)")
        return
    result.texts.append((path, text))


def _safe_member_path(name: str) -> str | None:
    name = name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    if any(p in SKIP_DIRS or (p.startswith(".") and p not in (".env.example",)) for p in parts[:-1]):
        return None
    return "/".join(parts)


def ingest_zip(data: bytes, result: Ingested) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        result.skipped.append("archive (not a valid zip)")
        return
    total = 0
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            result.skipped.append(f"archive ({len(entries)} entries - only the first {MAX_ZIP_ENTRIES} were read)")
            entries = entries[:MAX_ZIP_ENTRIES]
        for info in entries:
            if info.is_dir():
                continue
            path = _safe_member_path(info.filename)
            if path is None:
                result.skipped.append(f"{info.filename} (unsafe or ignored path)")
                continue
            if info.compress_size and info.file_size / max(1, info.compress_size) > MAX_COMPRESSION_RATIO:
                result.skipped.append(f"{path} (suspicious compression ratio)")
                continue
            total += info.file_size
            if total > MAX_ZIP_TOTAL:
                result.skipped.append("remaining files (archive too large)")
                break
            if info.file_size > max(MAX_TEXT_FILE_BYTES, MAX_IMAGE_BYTES):
                result.skipped.append(f"{path} (file too large)")
                continue
            with archive.open(info) as handle:
                _accept(result, path, handle.read(max(MAX_TEXT_FILE_BYTES, MAX_IMAGE_BYTES) + 1))


def ingest_upload(filename: str, data: bytes, result: Ingested) -> None:
    safe_name = PurePosixPath((filename or "file").replace("\\", "/")).name or "file"
    if safe_name.lower().endswith(".zip"):
        ingest_zip(data, result)
    else:
        _accept(result, safe_name, data)


def build_content(texts: list[tuple[str, str]], limit: int = MAX_CONTENT_CHARS) -> str:
    parts: list[str] = []
    used = 0
    for path, text in texts:
        block = f"### FILE: {path}\n```{language_for(path)}\n{text.rstrip()}\n```\n"
        if used + len(block) > limit:
            parts.append(f"### (truncated: remaining files omitted after {used} characters)\n")
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            newline = text.rfind("\n", start + size // 2, end)
            if newline != -1:
                end = newline
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def excerpt(content: str, budget_chars: int) -> str:
    if len(content) <= budget_chars:
        return content
    head = int(budget_chars * 0.75)
    tail = budget_chars - head
    return content[:head] + "\n\n... [truncated to fit the context-memory budget] ...\n\n" + content[-tail:]
