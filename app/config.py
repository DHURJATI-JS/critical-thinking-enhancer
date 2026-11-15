"""Configuration loading.

* ``config.json`` (or the file named by ``CTE_CONFIG``) is the single source of truth.
* Values may contain ``${ENV_VAR}`` / ``${ENV_VAR:-default}`` placeholders which are
  resolved from the process environment and from a git-ignored ``.env`` file, so the
  committed config never contains secrets.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT / ".runtime"
UPLOADS_ROOT = RUNTIME_DIR / "uploads"  # one sub-folder per deployment - see UPLOAD_DIR at the end of this module

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")


def _load_dotenv(path: Path) -> None:
    """Minimal .env parser (KEY=VALUE, # comments). Existing env vars win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():  # -sig: tolerate a BOM (Notepad / PowerShell)
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        # shell semantics: ${VAR:-default} uses default when VAR is unset OR empty
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


# --------------------------------------------------------------------------- models
class AppCfg(BaseModel):
    name: str = "Critical Thinking Enhancer"
    tagline: str = "Cognitive trainer for student developers"
    author: str = ""
    vercel_url: str = ""
    ai_server_repo: str = ""
    host: str = "0.0.0.0"
    port: int = 8010
    max_upload_mb: int = 20
    secret_key: str = ""
    # Family mode: the sign-in screen lists every local profile (name + avatar only). Set false on shared
    # networks (e.g. a classroom) - then only usernames remembered on that device are shown.
    list_profiles_on_signin: bool = True
    # ONE person uses the app at a time: others cannot sign in or do anything until that person signs out,
    # closes the app (no heartbeat for lock_heartbeat_seconds) or is idle for lock_idle_minutes.
    single_active_user: bool = True
    lock_heartbeat_seconds: int = 90
    lock_idle_minutes: int = 30


class DatabaseCfg(BaseModel):
    url: str
    pool_size: int = 10
    max_overflow: int = 20


class RedisCfg(BaseModel):
    enabled: bool = True
    url: str = "redis://127.0.0.1:6380/0"


class EndpointCfg(BaseModel):
    label: str = ""
    base_url: str
    api_key: str = ""
    model: str
    timeout_seconds: float = 120
    supports_json_mode: bool = True
    supports_logprobs: bool = False
    send_reasoning_effort: bool = False
    temperature: float | None = None
    enabled: str | bool = True
    max_concurrency: int | None = None  # cap parallel requests (e.g. llama.cpp slots behind a gateway)
    max_retries: int = 2  # OpenAI SDK retries (429 / 5xx / connection) with exponential backoff
    # AI-server gateways report their backends' slots: use ALL free slots when nobody else is using the server
    # (maximum speed), and leave room when other work is running on it. max_concurrency is the starting value.
    adaptive_concurrency: bool = False
    concurrency_ceiling: int = 16


class FallbackCfg(BaseModel):
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-sonnet-5-5"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5-mini"
    timeout_seconds: float = 120


class AICfg(BaseModel):
    gateway: EndpointCfg
    coder: EndpointCfg
    vision: EndpointCfg
    fallback: FallbackCfg = Field(default_factory=FallbackCfg)
    health_interval_seconds: float = 5
    health_timeout_seconds: float = 4
    confidence_threshold: float = 0.65
    confidence_max_retries: int = 2
    blocked_local_model_keywords: list[str] = Field(default_factory=lambda: ["gemma"])


class EmbeddingsCfg(BaseModel):
    provider: str = "fastembed"  # fastembed | openai_compatible | hashing
    model: str = "BAAI/bge-small-en-v1.5"
    dim: int = 384
    base_url: str = ""
    api_key: str = ""


class OcrCfg(BaseModel):
    layers: list[str] = Field(default_factory=lambda: ["tesseract", "rapidocr", "system", "vision_llm"])
    use_system_ocr: bool = True
    vision_llm_when_system_unavailable: bool = True
    vision_llm_as_last_resort: bool = True
    accept_confidence: float = 0.65
    tesseract_cmd: str = ""


class WorkersCfg(BaseModel):
    concurrency: int = 6
    job_timeout_seconds: float = 300


class SpamCfg(BaseModel):
    chat_per_minute: int = 12
    upload_per_minute: int = 6
    onboard_per_10min: int = 6
    block_threshold: float = 0.8
    ai_check_threshold: float = 0.45
    mute_after_blocks: int = 5
    mute_seconds: int = 120
    max_message_chars: int = 6000


class LevelsCfg(BaseModel):
    max_declines_before_lock: int = 3
    turns_between_offers: int = 3


class SecurityCfg(BaseModel):
    """HTTP hardening (app/security/talisman.py) and CSRF tokens (app/security/csrf.py)."""
    # Redirect http:// to https:// - only when the app sits behind TLS (it normally runs on plain HTTP in a LAN).
    force_https: bool = False
    # Strict-Transport-Security, sent on HTTPS responses only.
    hsts_max_age: int = 31_536_000
    hsts_include_subdomains: bool = True
    # Report-only mode logs CSP violations in the browser console without blocking (for debugging).
    csp_report_only: bool = False
    referrer_policy: str = "same-origin"
    frame_options: str = "DENY"
    # DNS-rebinding guard: IP addresses, localhost and this computer's own names always work. Add other names you
    # open the app with (e.g. "ctapp.lan", "*.home.arpa"); "*" disables the check (trusted reverse proxy).
    allowed_hosts: list[str] = Field(default_factory=list)
    # Extra origins allowed to POST (only needed behind a reverse proxy that rewrites the Host header).
    trusted_origins: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    app: AppCfg = Field(default_factory=AppCfg)
    database: DatabaseCfg
    redis: RedisCfg = Field(default_factory=RedisCfg)
    ai: AICfg
    embeddings: EmbeddingsCfg = Field(default_factory=EmbeddingsCfg)
    ocr: OcrCfg = Field(default_factory=OcrCfg)
    workers: WorkersCfg = Field(default_factory=WorkersCfg)
    spam: SpamCfg = Field(default_factory=SpamCfg)
    levels: LevelsCfg = Field(default_factory=LevelsCfg)
    security: SecurityCfg = Field(default_factory=SecurityCfg)
    config_path: str = ""


# --------------------------------------------------------------------------- helpers
def is_placeholder(value: str | None) -> bool:
    v = (value or "").strip()
    return not v or (v.startswith("<") and v.endswith(">"))


def is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in {"localhost", "host.docker.internal", "0.0.0.0"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return host.endswith(".local") or host.endswith(".lan")


def looks_blocked(settings: "Settings", role: str) -> bool:
    ep: EndpointCfg = getattr(settings.ai, role)
    return is_local_url(ep.base_url) and any(kw.lower() in ep.model.lower() for kw in settings.ai.blocked_local_model_keywords)


def blocked_model_reason(settings: "Settings", role: str, model_id: str | None = None, *, via_ai_server: bool = False) -> str | None:
    """Locally-run Gemma (or any configured keyword) is not allowed - users must use the AI server repo.

    A model served THROUGH the AI server gateway (detected via its /status backends list) is the
    sanctioned route, so it is never blocked.
    """
    if via_ai_server:
        return None
    ep: EndpointCfg = getattr(settings.ai, role)
    candidates = [ep.model] + ([model_id] if model_id else [])
    if not is_local_url(ep.base_url):
        return None
    for kw in settings.ai.blocked_local_model_keywords:
        for name in candidates:
            if name and kw.lower() in name.lower():
                repo = settings.app.ai_server_repo
                repo_txt = repo if not is_placeholder(repo) else "the AI server repo (see config.json -> app.ai_server_repo)"
                return (
                    f"Locally-run '{name}' is not supported for the {role} role. "
                    f"Please download and run {repo_txt} instead."
                )
    return None


def is_placeholder_secret(value: str | None) -> bool:
    """Empty, too short, or a documented placeholder such as .env.example's 'change-me-...'."""
    v = (value or "").strip()
    return len(v) < 16 or v.lower().startswith(("change-me", "changeme", "your-", "<"))


@lru_cache(maxsize=1)
def app_secret() -> str:
    """The server secret: CTE_SECRET_KEY, or a random one generated on first run and kept in .runtime/secret.key
    (owner-only permissions). A placeholder value is never used as a real secret."""
    secret = get_settings().app.secret_key
    if is_placeholder_secret(secret):
        if secret:
            logging.getLogger("cte").warning("CTE_SECRET_KEY is a placeholder - using a random secret in .runtime/secret.key instead")
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        key_file = RUNTIME_DIR / "secret.key"
        secret = key_file.read_text(encoding="utf-8").strip() if key_file.exists() else ""
        if is_placeholder_secret(secret):
            secret = secrets.token_urlsafe(48)
            key_file.write_text(secret, encoding="utf-8")
        try:
            os.chmod(key_file, 0o600)  # POSIX: owner only (no-op beyond read-only on Windows)
        except OSError:
            pass
    return secret


def derived_key(purpose: str) -> bytes:
    """A separate 32-byte key per purpose (HKDF-style HMAC), so no two features share key material."""
    return hmac.new(app_secret().encode("utf-8"), f"cte:{purpose}".encode("utf-8"), hashlib.sha256).digest()


def fernet_key() -> bytes:
    """Stable Fernet key derived from the app secret (unchanged derivation: existing encrypted keys stay readable)."""
    return base64.urlsafe_b64encode(hashlib.sha256(app_secret().encode("utf-8")).digest())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    path = Path(os.environ.get("CTE_CONFIG") or (ROOT / "config.json"))
    if not path.is_absolute():
        path = ROOT / path
    raw = json.loads(path.read_text(encoding="utf-8-sig"))  # tolerate a UTF-8 BOM written by Windows editors
    data = _interpolate(raw)
    data["config_path"] = str(path)
    return Settings.model_validate(data)


def deployment_namespace(database_url: str) -> str:
    """One deployment = one PostgreSQL database (host, port and name - never the password). Keeps apart what two
    deployments started from the same checkout (the app and the isolated test stack) would otherwise share:
    Redis pub/sub channels (global to the Redis SERVER, not per /db number) and the upload folder."""
    db = urlparse(database_url.replace("+asyncpg", ""))
    ident = f"{db.hostname}:{db.port}/{db.path.lstrip('/')}"
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:10]


def _deployment_id() -> str:
    try:
        return deployment_namespace(get_settings().database.url)
    except Exception:  # noqa: BLE001 - config unreadable: the app reports that itself when it starts
        return "default"


DEPLOYMENT_ID = _deployment_id()
# The janitor deletes upload leftovers whose project / job it cannot find in ITS OWN database, so deployments must
# never share this folder - otherwise a test run would delete the real app's screenshots waiting for "Analyse".
UPLOAD_DIR = UPLOADS_ROOT / DEPLOYMENT_ID
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def safe_link(url: str | None) -> str:
    """Only http(s) links may reach the browser (blocks javascript: / data: URLs from config)."""
    if is_placeholder(url):
        return ""
    parsed = urlparse(str(url).strip())
    return str(url).strip() if parsed.scheme in ("http", "https") and parsed.netloc else ""


def public_config() -> dict[str, Any]:
    """Non-secret config exposed to the browser."""
    s = get_settings()
    return {
        "name": s.app.name,
        "tagline": s.app.tagline,
        "author": s.app.author,
        "vercel_url": safe_link(s.app.vercel_url),
        "ai_server_repo": safe_link(s.app.ai_server_repo),
        "ai_server_repo_configured": bool(safe_link(s.app.ai_server_repo)),
        "confidence_threshold": s.ai.confidence_threshold,
        "health_interval_seconds": s.ai.health_interval_seconds,
        "max_upload_mb": s.app.max_upload_mb,
        "max_message_chars": s.spam.max_message_chars,
        "max_declines_before_lock": s.levels.max_declines_before_lock,
        "gateway_label": s.ai.gateway.label,
        "coder_label": s.ai.coder.label,
        "fallback_models": {
            "anthropic": s.ai.fallback.anthropic_model,
            "openai": s.ai.fallback.openai_model,
        },
    }
