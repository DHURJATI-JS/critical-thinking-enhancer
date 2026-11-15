"""Local accounts: password hashing, username/password rules and the single-active-user lock rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.security.passwords import hash_password, normalize_username, password_problem, username_problem, verify_password
from app.services import session_lock


def test_password_hash_roundtrip_and_salt():
    a, b = hash_password("correct horse battery"), hash_password("correct horse battery")
    assert a != b  # random salt per hash
    assert a.startswith("scrypt$") and "correct" not in a
    assert verify_password("correct horse battery", a)
    assert not verify_password("correct horse batterY", a)


@pytest.mark.parametrize("stored", [None, "", "md5$abc", "scrypt$1$2", "scrypt$x$y$z$!!$??"])
def test_verify_never_accepts_missing_or_broken_hashes(stored):
    assert verify_password("anything-at-all", stored) is False


@pytest.mark.parametrize("raw,ok", [("alice", True), ("  Alice ", True), ("a.b-c_d", True), ("ab", False), ("-alice", False),
                                    ("alice\n", True), ("ali ce", False), ("alïce", False), ("x" * 33, False), ("", False)])
def test_username_rules(raw, ok):
    assert (username_problem(normalize_username(raw)) is None) is ok


def test_username_rule_rejects_trailing_newline_without_normalizing():
    assert username_problem("alice\n") is not None


@pytest.mark.parametrize("pw,user,ok", [("short", "", False), ("long-enough", "", True), ("Alice-1234", "alice-1234", False),
                                        ("x" * 129, "", False)])
def test_password_rules(pw, user, ok):
    assert (password_problem(pw, user) is None) is ok


def _lock(heartbeat_ago: float, activity_ago: float) -> dict:
    now = datetime.now(timezone.utc)
    return {"user_id": "u1", "name": "Ada Lovelace", "username": "ada", "since": now.isoformat(),
            "heartbeat": (now - timedelta(seconds=heartbeat_ago)).isoformat(),
            "activity": (now - timedelta(seconds=activity_ago)).isoformat()}


def test_lock_expires_on_missing_heartbeat_or_idle():
    now = datetime.now(timezone.utc)
    assert not session_lock._expired(_lock(5, 60), now)
    assert session_lock._expired(_lock(600, 60), now)          # tab closed: no heartbeat
    assert session_lock._expired(_lock(5, 31 * 60), now)       # stepped away: heartbeats but no interaction
    assert session_lock._expired({"user_id": "u1"}, now)        # malformed -> treated as free
    assert session_lock._expired({"heartbeat": "nope", "activity": "nope"}, now)


def test_lock_public_view_shows_first_name_only():
    view = session_lock.public_view(_lock(5, 5))
    assert view["locked"] is True and view["name"] == "Ada"
    assert "user_id" not in view and "heartbeat" not in view and "activity" not in view
    assert session_lock.public_view(None) == {"locked": False}
