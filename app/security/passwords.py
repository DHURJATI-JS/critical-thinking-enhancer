"""Password hashing with scrypt (Python standard library - memory-hard, salted, no extra dependency)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets

_N, _R, _P, _DKLEN = 2**14, 8, 1, 32
USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{2,31}")  # always used with fullmatch
# Used to spend the same time on unknown usernames as on real ones (no username probing by timing).
_DUMMY = None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str | None) -> bool:
    global _DUMMY
    if not stored:
        _DUMMY = _DUMMY or hash_password("not-a-real-password")
        stored, real = _DUMMY, False
    else:
        real = True
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected) and real


async def hash_password_async(password: str) -> str:
    """scrypt takes ~50-100 ms of CPU: run it in a worker thread so the server keeps answering everyone else."""
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, stored: str | None) -> bool:
    return await asyncio.to_thread(verify_password, password, stored)


def normalize_username(raw: str) -> str:
    return (raw or "").strip().lower()


def username_problem(username: str) -> str | None:
    if not USERNAME_RE.fullmatch(username or ""):
        return "Usernames are 3-32 characters: lowercase letters, digits, dots, dashes or underscores (start with a letter or digit)."
    return None


def password_problem(password: str, username: str = "") -> str | None:
    if len(password) < 8:
        return "Use at least 8 characters for your password."
    if len(password) > 128:
        return "That password is too long (max 128 characters)."
    if username and password.lower() == username.lower():
        return "Your password can't be the same as your username."
    return None
