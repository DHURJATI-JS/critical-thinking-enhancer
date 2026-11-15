"""Hardware detection (CPU / RAM / GPU vendor + VRAM) and automatic context-memory budgeting.

The context-memory budget available to the AI is derived automatically from this device:
GPU VRAM decides which Qwen coder size and ``max_model_len`` vLLM can serve, and the live
``max_model_len`` reported by the model servers caps it further.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from typing import Any

import psutil

from ..config import get_settings, is_local_url

_CACHE: dict[str, Any] | None = None

# (min VRAM GB, coder model, max_model_len, tier)
CODER_TIERS: list[tuple[float, str, int, str]] = [
    (40, "Qwen/Qwen2.5-Coder-32B-Instruct-AWQ", 32768, "high"),
    (20, "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ", 32768, "high"),
    (12, "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ", 16384, "mid"),
    (8, "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ", 8192, "mid"),
    (6, "Qwen/Qwen2.5-Coder-3B-Instruct-AWQ", 8192, "low"),
]
TIER_BUDGET = {"high": 32768, "mid": 16384, "low": 8192, "insufficient": 4096}
MIN_RAM_GB = 8
MIN_VRAM_GB = 6


def _run(cmd: list[str], timeout: float = 10) -> str:
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        out = subprocess.run(cmd, **kwargs)  # noqa: S603 - fixed argument lists only
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _vendor(name: str) -> str | None:
    n = name.lower()
    if any(k in n for k in ("nvidia", "geforce", "rtx", "quadro", "tesla")):
        return "nvidia"
    if any(k in n for k in ("amd", "radeon", "instinct")):
        return "amd"
    if "intel" in n and any(k in n for k in ("arc", "iris", "uhd", "graphics")):
        return "intel"
    if "apple" in n:
        return "apple"
    return None


def _nvidia_gpus() -> list[dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return []
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            try:
                vram = round(float(parts[1]) / 1024, 1)
            except ValueError:
                vram = None
            gpus.append({"vendor": "nvidia", "name": parts[0], "vram_gb": vram, "driver": parts[2] if len(parts) > 2 else ""})
    return gpus


_WIN_GPU_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$reg = Get-ChildItem 'HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}' |
  ForEach-Object { Get-ItemProperty $_.PSPath } |
  Where-Object { $_.'HardwareInformation.qwMemorySize' } |
  ForEach-Object { [pscustomobject]@{ name = [string]$_.DriverDesc; bytes = [int64]$_.'HardwareInformation.qwMemorySize' } }
$wmi = Get-CimInstance Win32_VideoController |
  ForEach-Object { [pscustomobject]@{ name = [string]$_.Name; bytes = [int64]$_.AdapterRAM; driver = [string]$_.DriverVersion } }
@{ reg = @($reg); wmi = @($wmi) } | ConvertTo-Json -Depth 4 -Compress
"""


def _windows_gpus() -> list[dict[str, Any]]:
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WIN_GPU_SCRIPT], timeout=20)
    try:
        data = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        return []
    sizes: dict[str, int] = {}
    drivers: dict[str, str] = {}
    for row in (data.get("wmi") or []) + (data.get("reg") or []):
        if not isinstance(row, dict) or not row.get("name"):
            continue
        name = row["name"].strip()
        sizes[name] = max(sizes.get(name, 0), int(row.get("bytes") or 0))
        if row.get("driver"):
            drivers[name] = row["driver"]
    gpus = []
    for name, size in sizes.items():
        vendor = _vendor(name)
        if vendor:
            gpus.append({"vendor": vendor, "name": name, "vram_gb": round(size / 1024**3, 1) if size else None, "driver": drivers.get(name, "")})
    return gpus


def _linux_gpus() -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    if shutil.which("rocm-smi"):
        out = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"])
        try:
            data = json.loads(out) if out.strip() else {}
        except json.JSONDecodeError:
            data = {}
        for card in data.values() if isinstance(data, dict) else []:
            if not isinstance(card, dict):
                continue
            name = card.get("Card series") or card.get("Card SKU") or card.get("Card model") or "AMD GPU"
            total = card.get("VRAM Total Memory (B)")
            vram = round(int(total) / 1024**3, 1) if total and str(total).isdigit() else None
            gpus.append({"vendor": "amd", "name": str(name), "vram_gb": vram, "driver": "rocm"})
    if not gpus and shutil.which("lspci"):
        for line in _run(["lspci"]).splitlines():
            if any(k in line for k in ("VGA", "3D controller", "Display controller")):
                name = line.split(":", 2)[-1].strip()
                vendor = _vendor(name)
                if vendor in ("amd", "intel", "nvidia"):
                    gpus.append({"vendor": vendor, "name": name, "vram_gb": None, "driver": ""})
    return gpus


def detect_hardware(force: bool = False) -> dict[str, Any]:
    """Blocking (subprocess) detection - call via ``asyncio.to_thread`` from async code."""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    system = platform.system()
    ram_gb = round(psutil.virtual_memory().total / 1024**3, 1)
    gpus = _nvidia_gpus()
    if not gpus:
        if system == "Windows":
            gpus = _windows_gpus()
        elif system == "Linux":
            gpus = _linux_gpus()
        elif system == "Darwin" and platform.machine() == "arm64":
            gpus = [{"vendor": "apple", "name": "Apple Silicon GPU (unified memory)", "vram_gb": ram_gb, "driver": "metal"}]
    compute = [g for g in gpus if g["vendor"] in ("nvidia", "amd")]
    best = max(compute, key=lambda g: g.get("vram_gb") or 0, default=None)
    vram = (best or {}).get("vram_gb") or 0
    tier, coder_model, max_len = "insufficient", None, TIER_BUDGET["insufficient"]
    for min_vram, model, length, t in CODER_TIERS:
        if vram >= min_vram:
            tier, coder_model, max_len = t, model, length
            break
    vllm_gpu_supported = bool(best) and (best["vendor"] == "nvidia" or (best["vendor"] == "amd" and system == "Linux"))
    _CACHE = {
        "os": f"{system} {platform.release()}",
        "system": system,
        "machine": platform.machine(),
        "cpu": platform.processor() or platform.machine(),
        "cpu_cores": psutil.cpu_count(logical=True),
        "ram_gb": ram_gb,
        "gpus": gpus,
        "primary_gpu": best,
        "gpu_vendor": (best or {}).get("vendor"),
        "vram_gb": vram,
        "tier": tier,
        "recommended_coder_model": coder_model,
        "recommended_max_model_len": max_len,
        "vllm_gpu_supported": vllm_gpu_supported,
    }
    return _CACHE


def hardware() -> dict[str, Any]:
    return _CACHE or detect_hardware()


def context_budget(role: str, reported_max_len: int | None) -> int:
    """Tokens of context memory the given AI role can use on this setup."""
    settings = get_settings()
    endpoint = getattr(settings.ai, role)
    local = is_local_url(endpoint.base_url)
    hw = hardware()
    tier_budget = TIER_BUDGET.get(hw["tier"], 4096)
    if reported_max_len:
        return min(int(reported_max_len), tier_budget) if (local and role == "coder") else int(reported_max_len)
    return tier_budget if local else 16384


def low_end_report(roles_state: dict[str, dict] | None = None) -> dict[str, Any]:
    hw = hardware()
    settings = get_settings()
    reasons: list[str] = []
    notes: list[str] = []
    if hw["ram_gb"] < MIN_RAM_GB:
        reasons.append(f"Only {hw['ram_gb']} GB RAM detected (minimum {MIN_RAM_GB} GB recommended).")
    coder_local = is_local_url(settings.ai.coder.base_url)
    coder_state = (roles_state or {}).get("coder") or {}
    coder_running = bool(coder_state.get("ok"))
    if coder_local and not coder_running:  # only nag when the local coder is actually down
        if hw["vram_gb"] < MIN_VRAM_GB:
            reasons.append(
                f"No GPU with at least {MIN_VRAM_GB} GB VRAM was found for the local Qwen coder "
                f"(best: {hw['primary_gpu']['name'] + ' ' + str(hw['vram_gb']) + ' GB' if hw['primary_gpu'] else 'none'})."
            )
        elif not hw["vllm_gpu_supported"]:
            notes.append(
                f"The Qwen coder is not running. On {hw['system']} with {hw['primary_gpu']['name']} it runs on your GPU inside "
                "Docker through llama.cpp (Vulkan) - start it with `docker compose up -d` (the installer sets this up)."
            )
    budget = context_budget("coder", coder_state.get("max_model_len"))
    if budget < 4096:
        reasons.append(f"Context memory budget is only {budget} tokens.")
    return {
        "low_end": bool(reasons),
        "reasons": reasons,
        "notes": notes,
        "budget_tokens": budget,
        "advice": (
            "Your device is too low-end for comfortable local AI. Please upgrade your setup "
            "(GPU with 8 GB+ VRAM and 16 GB RAM recommended) or add your own Claude or ChatGPT API key "
            "in Settings > AI fallback to continue."
        ),
    }
