"""Password hashing (bcrypt) and JWT tokens (HS256)."""

from __future__ import annotations

import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import bcrypt
import jwt

from .errors import Unauthorized, ValidationFailed

_ALPHABET = string.ascii_letters + string.digits
@lru_cache(maxsize=4)
def _dummy_hash(rounds: int) -> str:
    return bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(rounds=rounds)).decode()


def burn_password_check(password: str, rounds: int = 12) -> None:
    """Verify against a dummy hash when the email is unknown, so 'no such user' takes as long as
    'wrong password' (prevents account enumeration by timing)."""
    verify_password(password, _dummy_hash(rounds))


def hash_password(password: str, rounds: int = 12) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def generate_temp_password(length: int = 10) -> str:
    """Temporary password handed to a new user; must be changed on first sign-in."""
    core = "".join(secrets.choice(_ALPHABET) for _ in range(length - 2))
    return f"{core}{secrets.choice(string.digits)}!"


def validate_password_strength(password: str) -> None:
    problems = []
    if len(password) < 8:
        problems.append("at least 8 characters")
    if not any(c.isalpha() for c in password):
        problems.append("at least one letter")
    if not any(c.isdigit() for c in password):
        problems.append("at least one digit")
    if problems:
        raise ValidationFailed("Password is too weak: needs " + ", ".join(problems), code="weak_password")


def create_access_token(*, user_id: int, role: str, token_version: int, secret: str, expires_minutes: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "tv": token_version,
        "jti": uuid.uuid4().hex,  # lets one session be signed out without touching the others
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(token: str, secret: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("Session expired, please sign in again", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("Invalid token") from exc
