"""Symmetric encryption for secrets at rest (user-supplied fallback API keys)."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from ..config import fernet_key

_fernet: Fernet | None = None


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(fernet_key())
    return _fernet


def encrypt(value: str) -> str:
    return _f().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _f().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def mask(secret: str) -> str:
    return f"{secret[:5]}...{secret[-4:]}" if len(secret) > 12 else "..."
