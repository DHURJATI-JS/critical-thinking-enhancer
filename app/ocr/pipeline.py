"""Multi-layer OCR engine.

Order (configurable in config.json -> ocr.layers):
  1. Tesseract          (pytesseract, word-level confidence, indentation reconstructed)
  2. RapidOCR           (ONNX PP-OCR models, line-level confidence)
  3. System OCR         (Windows.Media.Ocr on Windows, Apple Vision on macOS) - optional
  4. MiniCPM-V on vLLM  (vision LLM, temperature ~0.05 to prevent hallucinations) - used when
                        system OCR is unavailable, and optionally as a last resort.
Each layer's result is scored; the first result >= ocr.accept_confidence (65 %) wins.
Otherwise the best result is returned flagged ``low_confidence`` (shown with a red warning).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import platform
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from ..config import get_settings

log = logging.getLogger("cte.ocr")
WINOCR_SCRIPT = Path(__file__).with_name("winocr.ps1")
_rapid_engine: Any = None
_rapid_lock = threading.Lock()
_system_probe: dict[str, Any] | None = None


# --------------------------------------------------------------------------- helpers
def text_quality(text: str) -> float:
    """Heuristic 0..1 plausibility of OCR output (used when an engine reports no confidence)."""
    t = (text or "").strip()
    if len(t) < 3:
        return 0.0
    printable = sum(ch.isprintable() or ch in "\n\t" for ch in t) / len(t)
    alnum = sum(ch.isalnum() for ch in t) / len(t)
    weird = len(re.findall(r"[^\w\s.,;:!?'\"()\[\]{}<>=+\-*/\\|&%$#@^~`]", t)) / len(t)
    words = re.findall(r"[^\W\d_]{2,}", t)
    avg_len = statistics.mean(len(w) for w in words) if words else 0.0
    word_ok = 1.0 if 2.5 <= avg_len <= 11 else 0.5
    score = 0.2 * printable + 0.4 * min(1.0, alnum / 0.55) + 0.2 * word_ok + 0.2 * max(0.0, 1 - weird * 5)
    if not words:  # symbols / digits only - almost certainly OCR noise
        score = min(score, 0.3)
    return round(max(0.0, min(1.0, score)), 3)


def _reindent(lines: list[tuple[float, float, str]]) -> str:
    """lines = [(left_px, char_width_px, text)] -> text with reconstructed leading indentation."""
    if not lines:
        return ""
    left_min = min(left for left, _, _ in lines)
    widths = [w for _, w, _ in lines if w > 0]
    char_w = statistics.median(widths) if widths else 8.0
    out = []
    for left, _, text in lines:
        spaces = int(round((left - left_min) / max(char_w, 1.0)))
        out.append(" " * min(spaces, 40) + text)
    return "\n".join(out)


# Only plain raster formats - never EPS/PS/PDF (Ghostscript), PSD, ICO tricks, ... whatever the file is named.
ALLOWED_IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "BMP", "TIFF", "GIF")
# A 20 KB PNG can declare 13000x13000 pixels and need ~3 GB to decode ("decompression bomb"). 30 MP is far more
# than any screenshot or phone photo of code needs for OCR.
MAX_IMAGE_PIXELS = 30_000_000
MAX_OCR_SIDE = 4096
# at most two images are decoded / OCR'd at the same time across the whole app (bounded memory)
_OCR_SLOTS = asyncio.Semaphore(2)


def image_problem(data: bytes) -> str | None:
    """Cheap header-only check (no pixel decoding): None when the image is safe to OCR."""
    try:
        with Image.open(io.BytesIO(data), formats=ALLOWED_IMAGE_FORMATS) as probe:
            width, height = probe.size
    except Exception:  # noqa: BLE001 - not an image we accept
        return "not a PNG/JPEG/WebP/BMP/TIFF/GIF image"
    if width * height > MAX_IMAGE_PIXELS:
        return f"image too large ({width}x{height} px, max {MAX_IMAGE_PIXELS // 1_000_000} megapixels)"
    return None


def load_image(data: bytes) -> Image.Image:
    problem = image_problem(data)
    if problem:
        raise ValueError(problem)
    image = Image.open(io.BytesIO(data), formats=ALLOWED_IMAGE_FORMATS)
    if image.format == "JPEG":
        image.draft("RGB", (MAX_OCR_SIDE, MAX_OCR_SIDE))  # decode big photos at a reduced scale
    image.load()
    if max(image.size) > MAX_OCR_SIDE:
        image.thumbnail((MAX_OCR_SIDE, MAX_OCR_SIDE))
    if getattr(image, "is_animated", False):
        image.seek(0)
    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "L"):
        background = Image.new("RGB", image.size, "white")
        background.paste(image.convert("RGBA"), mask=image.convert("RGBA").split()[-1])
        image = background
    return image.convert("RGB")


def _preprocess(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    if gray.width < 1400:
        factor = min(3.0, 1400 / max(1, gray.width))
        gray = gray.resize((int(gray.width * factor), int(gray.height * factor)), Image.LANCZOS)
    # dark-theme screenshots (light text on dark background) -> invert for Tesseract
    if statistics.mean(gray.resize((64, 64)).getdata()) < 110:
        gray = ImageOps.invert(gray)
    return ImageOps.autocontrast(gray)


# --------------------------------------------------------------------------- engines
def tesseract_cmd() -> str | None:
    configured = get_settings().ocr.tesseract_cmd
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                      "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
        if Path(candidate).exists():
            return candidate
    return None


def _tesseract(image: Image.Image) -> dict[str, Any]:
    cmd = tesseract_cmd()
    if not cmd:
        return {"ok": False, "error": "Tesseract is not installed"}
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = cmd
    data = pytesseract.image_to_data(
        _preprocess(image), config="--psm 6 -c preserve_interword_spaces=1", output_type=pytesseract.Output.DICT
    )
    rows: dict[tuple[int, int, int], list[tuple[int, int, str]]] = {}
    confs: list[float] = []
    for i, word in enumerate(data["text"]):
        if not str(word).strip():
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if conf >= 0:
            confs.append(conf)
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        rows.setdefault(key, []).append((int(data["left"][i]), int(data["width"][i]), str(word)))
    lines = []
    for key in sorted(rows):
        words = sorted(rows[key])
        first_left, first_width, first_word = words[0]
        lines.append((float(first_left), first_width / max(1, len(first_word)), " ".join(w for _, _, w in words)))
    text = _reindent(lines)
    confidence = (sum(confs) / len(confs) / 100.0) if confs else 0.0
    return {"ok": bool(text.strip()), "text": text, "confidence": round(confidence, 3)}


def _rapidocr(image: Image.Image) -> dict[str, Any]:
    global _rapid_engine
    try:
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return {"ok": False, "error": "RapidOCR is not installed"}
    with _rapid_lock:
        if _rapid_engine is None:
            _rapid_engine = RapidOCR()
        result, _ = _rapid_engine(np.array(image))
    if not result:
        return {"ok": False, "error": "no text detected", "text": "", "confidence": 0.0}
    items = []
    for box, text, score in result:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append({"top": min(ys), "bottom": max(ys), "left": min(xs), "right": max(xs), "text": str(text), "score": float(score)})
    heights = [it["bottom"] - it["top"] for it in items]
    tolerance = (statistics.median(heights) if heights else 10) * 0.5
    items.sort(key=lambda it: (it["top"], it["left"]))
    grouped: list[list[dict[str, Any]]] = []
    for it in items:
        if grouped and abs(grouped[-1][0]["top"] - it["top"]) <= tolerance:
            grouped[-1].append(it)
        else:
            grouped.append([it])
    lines = []
    for group in grouped:
        group.sort(key=lambda it: it["left"])
        first = group[0]
        char_w = (first["right"] - first["left"]) / max(1, len(first["text"]))
        lines.append((first["left"], char_w, " ".join(g["text"] for g in group)))
    scores = [it["score"] for it in items]
    return {"ok": True, "text": _reindent(lines), "confidence": round(sum(scores) / len(scores), 3)}


def _run_quiet(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    kwargs: dict[str, Any] = {"capture_output": True, "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000
    return subprocess.run(cmd, **kwargs)  # noqa: S603 - fixed arguments


def system_ocr_probe() -> dict[str, Any]:
    """Which OS-native OCR exists here? Cached; blocking."""
    global _system_probe
    if _system_probe is not None:
        return _system_probe
    system = platform.system()
    probe: dict[str, Any] = {"available": False, "engine": None, "detail": ""}
    if system == "Windows":
        try:
            out = _run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(WINOCR_SCRIPT), "-Probe"], 25)
            data = json.loads(out.stdout.decode("utf-8", "replace").strip().lstrip("\ufeff") or "{}")
            probe = {"available": bool(data.get("ok")), "engine": "windows", "detail": data.get("language") or data.get("error", ""),
                     "max_dim": data.get("max_dim")}
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            probe = {"available": False, "engine": "windows", "detail": f"probe failed: {exc}"}
    elif system == "Darwin":
        try:
            import ocrmac  # noqa: F401

            probe = {"available": True, "engine": "apple_vision", "detail": "Apple Vision via ocrmac"}
        except ImportError:
            probe = {"available": False, "engine": "apple_vision", "detail": "pip install ocrmac to enable Apple Vision"}
    else:
        probe = {"available": False, "engine": None, "detail": f"no OS-native OCR on {system}"}
    _system_probe = probe
    return probe


def _windows_ocr(image: Image.Image) -> dict[str, Any]:
    probe = system_ocr_probe()
    max_dim = int(probe.get("max_dim") or 2600)
    img = image.copy()
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim))
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        img.save(tmp, "PNG")
        out = _run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(WINOCR_SCRIPT), "-Path", tmp], 60)
        data = json.loads(out.stdout.decode("utf-8", "replace").strip().lstrip("\ufeff") or "{}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", "Windows OCR failed")}
    text = data.get("text", "")
    return {"ok": bool(text.strip()), "text": text, "confidence": round(min(0.9, text_quality(text)), 3), "confidence_source": "heuristic"}


def _apple_vision(image: Image.Image) -> dict[str, Any]:
    try:
        from ocrmac import ocrmac
    except ImportError:
        return {"ok": False, "error": "ocrmac not installed"}
    annotations = ocrmac.OCR(image, recognition_level="accurate").recognize()
    if not annotations:
        return {"ok": False, "error": "no text detected"}
    rows = sorted(annotations, key=lambda a: (-round(a[2][1], 2), a[2][0]))  # bbox origin is bottom-left
    text = "\n".join(a[0] for a in rows)
    conf = sum(float(a[1]) for a in rows) / len(rows)
    return {"ok": True, "text": text, "confidence": round(conf, 3)}


async def _vision_llm(image: Image.Image, user: Any) -> dict[str, Any]:
    from ..ai import router
    from ..ai.prompts import VISION_OCR_PROMPT

    settings = get_settings()
    img = image.copy()
    img.thumbnail((1600, 1600))
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    messages = [{"role": "user", "content": [{"type": "text", "text": VISION_OCR_PROMPT}, {"type": "image_url", "image_url": {"url": url}}]}]
    temperature = settings.ai.vision.temperature if settings.ai.vision.temperature is not None else 0.05
    result = await router.chat("vision", messages, user=user, temperature=temperature, max_tokens=2048, logprobs=True)
    text = re.sub(r"^```\w*\n|\n```$", "", result.text.strip())
    quality = text_quality(text)
    conf = quality if result.token_confidence is None else 0.5 * quality + 0.5 * result.token_confidence
    if "[unreadable]" in text:
        conf *= 0.8
    return {"ok": bool(text.strip()), "text": text, "confidence": round(conf, 3), "model": result.model}


# --------------------------------------------------------------------------- pipeline
def engines_status() -> dict[str, Any]:
    from ..ai.health import monitor

    probe = system_ocr_probe()
    try:
        import rapidocr_onnxruntime  # noqa: F401

        rapid = True
    except ImportError:
        rapid = False
    return {
        "order": get_settings().ocr.layers,
        "accept_confidence": get_settings().ocr.accept_confidence,
        "tesseract": {"available": tesseract_cmd() is not None, "path": tesseract_cmd()},
        "rapidocr": {"available": rapid},
        "system": {**probe, "enabled": get_settings().ocr.use_system_ocr},
        "vision_llm": {
            "enabled": monitor.vision_enabled(),
            "online": monitor.role_ok("vision"),
            "used_when": "system OCR unavailable" + (" + as last resort" if get_settings().ocr.vision_llm_as_last_resort else ""),
            "temperature": get_settings().ai.vision.temperature,
        },
    }


async def run_ocr(data: bytes, *, user: Any = None) -> dict[str, Any]:
    async with _OCR_SLOTS:  # bounded memory: never more than two images in flight
        return await _run_ocr(data, user=user)


async def _run_ocr(data: bytes, *, user: Any = None) -> dict[str, Any]:
    from ..ai.health import monitor
    from ..ai.router import fallback_ready

    cfg = get_settings().ocr
    image = await asyncio.to_thread(load_image, data)
    probe = await asyncio.to_thread(system_ocr_probe)
    system_ready = cfg.use_system_ocr and probe["available"]
    layers: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None

    for layer in cfg.layers:
        started = time.perf_counter()
        try:
            if layer == "tesseract":
                res = await asyncio.to_thread(_tesseract, image)
            elif layer == "rapidocr":
                res = await asyncio.to_thread(_rapidocr, image)
            elif layer == "system":
                if not system_ready:
                    layers.append({"engine": "system", "skipped": probe.get("detail") or "not available"})
                    continue
                fn = _windows_ocr if probe["engine"] == "windows" else _apple_vision
                res = await asyncio.to_thread(fn, image)
                res["engine_detail"] = probe["engine"]
            elif layer == "vision_llm":
                wanted = (not system_ready and cfg.vision_llm_when_system_unavailable) or cfg.vision_llm_as_last_resort
                if not wanted:
                    layers.append({"engine": "vision_llm", "skipped": "system OCR available"})
                    continue
                if not monitor.vision_enabled() and not fallback_ready(user):
                    layers.append({"engine": "vision_llm", "skipped": "vision model disabled"})
                    continue
                if not monitor.role_ok("vision") and not fallback_ready(user):
                    layers.append({"engine": "vision_llm", "skipped": "MiniCPM-V server offline"})
                    continue
                res = await _vision_llm(image, user)
            else:
                continue
        except Exception as exc:  # noqa: BLE001 - a failing layer just hands over to the next one
            log.warning("OCR layer %s failed: %s", layer, exc)
            res = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"[:200]}
        entry = {
            "engine": layer,
            "ok": bool(res.get("ok")),
            "confidence": res.get("confidence"),
            "chars": len(res.get("text") or ""),
            "ms": int((time.perf_counter() - started) * 1000),
        }
        if res.get("error"):
            entry["error"] = res["error"]
        if res.get("engine_detail"):
            entry["detail"] = res["engine_detail"]
        layers.append(entry)
        if res.get("ok") and (best is None or float(res.get("confidence") or 0) > float(best["confidence"] or 0)):
            best = {**res, "engine": layer}
        if res.get("ok") and float(res.get("confidence") or 0) >= cfg.accept_confidence:
            break

    if best is None:
        return {"text": "", "engine": None, "confidence": 0.0, "low_confidence": True, "layers": layers}
    confidence = float(best.get("confidence") or 0)
    return {
        "text": best.get("text", ""),
        "engine": best["engine"],
        "confidence": round(confidence, 3),
        "low_confidence": confidence < cfg.accept_confidence,
        "layers": layers,
    }
