"""M1 — Auth & access: login (JWT + bcrypt), change password, principal resolution,
and Admin user management (create HR logins, deactivate, reset)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select

from ..context import Ctx, Principal, require_roles
from ..errors import Conflict, Forbidden, NotFound, RateLimited, Unauthorized, ValidationFailed
from ..models import Employee, LoginAttempt, RevokedToken, Role, User
from ..security import (
    burn_password_check,
    create_access_token,
    decode_access_token,
    generate_temp_password,
    hash_password,
    validate_password_strength,
    verify_password,
)
from . import audit
from .employees import EmployeeInput, create_employee_with_login


def _principal_for(ctx: Ctx, user: User) -> Principal:
    employee_id = ctx.db.scalar(select(Employee.id).where(Employee.user_id == user.id))
    return Principal(
        user_id=user.id, role=Role(user.role), email=user.email, full_name=user.full_name, employee_id=employee_id
    )


def _issue_token(ctx: Ctx, user: User) -> str:
    return create_access_token(
        user_id=user.id,
        role=user.role,
        token_version=user.token_version,
        secret=ctx.settings.jwt_secret,
        expires_minutes=ctx.settings.jwt_expires_minutes,
    )


def session_payload(ctx: Ctx, user: User) -> dict[str, Any]:
    principal = _principal_for(ctx, user)
    return {
        "access_token": _issue_token(ctx, user),
        "token_type": "bearer",
        "must_change_password": user.must_change_password,
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "employee_id": principal.employee_id,
        },
    }


GENERIC_LOGIN_ERROR = "Invalid email or password"


def _throttle_state(ctx: Ctx, email: str, ip: str) -> tuple[int, int, datetime | None]:
    """Failures in the sliding window: per email (reset by a successful sign-in) and per IP (never reset,
    so an attacker can't clear it by signing in to their own account). Returns the oldest counted failure."""
    since = ctx.now - timedelta(minutes=ctx.settings.login_window_minutes)
    last_ok_id = ctx.db.scalar(select(func.max(LoginAttempt.id)).where(
        LoginAttempt.email == email, LoginAttempt.success.is_(True))) or 0
    email_fail = select(LoginAttempt.created_at).where(
        LoginAttempt.email == email, LoginAttempt.success.is_(False), LoginAttempt.created_at >= since,
        LoginAttempt.id > last_ok_id)
    ip_fail = select(LoginAttempt.created_at).where(
        LoginAttempt.ip == ip, LoginAttempt.success.is_(False), LoginAttempt.created_at >= since)
    email_times = list(ctx.db.scalars(email_fail))
    ip_times = list(ctx.db.scalars(ip_fail))
    oldest = None
    if len(email_times) >= ctx.settings.login_max_failures:
        oldest = sorted(email_times)[-ctx.settings.login_max_failures]
    if len(ip_times) >= ctx.settings.login_max_failures_per_ip:
        ip_oldest = sorted(ip_times)[-ctx.settings.login_max_failures_per_ip]
        oldest = max(oldest, ip_oldest) if oldest else ip_oldest
    return len(email_times), len(ip_times), oldest


def login(ctx: Ctx, email: str, password: str, ip: str = "unknown") -> dict[str, Any]:
    """Generic error for every failure (unknown email, wrong password, inactive account) and throttling
    after repeated failures. Failed attempts are committed even though the request then fails."""
    email = email.strip().lower()
    ctx.db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < ctx.now - timedelta(days=1)))
    _, _, locked_since = _throttle_state(ctx, email, ip)
    if locked_since is not None:
        retry = int((locked_since + timedelta(minutes=ctx.settings.login_window_minutes) - ctx.now).total_seconds()) + 1
        raise RateLimited(f"Too many failed sign-in attempts. Try again in {max(1, (retry + 59) // 60)} minute(s).",
                          code="login_throttled", details={"retry_after_seconds": max(retry, 1)})
    user = ctx.db.scalar(select(User).where(User.email == email))
    if user is None:
        burn_password_check(password, ctx.settings.bcrypt_rounds)
        ok = False
    else:
        ok = verify_password(password, user.password_hash) and user.is_active
    ctx.db.add(LoginAttempt(email=email, ip=ip, success=ok, created_at=ctx.now))
    if not ok:
        ctx.db.flush()
        email_failures, _, now_locked = _throttle_state(ctx, email, ip)
        if now_locked is not None and email_failures == ctx.settings.login_max_failures:
            audit.record(ctx, "lockout", "user_login", None, after={"email": email, "ip": ip})
        ctx.db.commit()  # security events must persist although this request fails
        raise Unauthorized(GENERIC_LOGIN_ERROR, code="invalid_credentials")
    user.last_login_at = ctx.now
    return session_payload(ctx, user)


def principal_from_token(ctx: Ctx, token: str) -> tuple[Principal, User, dict[str, Any]]:
    payload = decode_access_token(token, ctx.settings.jwt_secret)
    user = ctx.db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise Unauthorized("Account not found or deactivated")
    if int(payload.get("tv", -1)) != user.token_version:
        raise Unauthorized("Session was revoked, please sign in again", code="token_revoked")
    jti = payload.get("jti")
    if not jti or ctx.db.scalar(select(RevokedToken.id).where(RevokedToken.jti == jti)):
        raise Unauthorized("You were signed out, please sign in again", code="token_revoked")
    return _principal_for(ctx, user), user, payload


def logout(ctx: Ctx, *, everywhere: bool = False) -> None:
    """Server-side sign-out. This session: its `jti` is denylisted until it expires.
    Everywhere: the user's token_version is bumped, which invalidates every issued token."""
    principal = ctx.actor
    claims = ctx.token_claims or {}
    user = ctx.db.get(User, principal.user_id)
    assert user is not None
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    ctx.db.execute(delete(RevokedToken).where(RevokedToken.expires_at < now_utc))  # housekeeping
    if everywhere:
        user.token_version += 1
    elif claims.get("jti"):
        expires = datetime.fromtimestamp(int(claims["exp"]), timezone.utc).replace(tzinfo=None)
        ctx.db.add(RevokedToken(jti=claims["jti"], user_id=user.id, expires_at=expires, revoked_at=ctx.now))
    audit.record(ctx, "logout", "user_session", user.id, after={"everywhere": everywhere})


def change_password(ctx: Ctx, current_password: str, new_password: str) -> dict[str, Any]:
    principal = ctx.actor
    user = ctx.db.get(User, principal.user_id)
    assert user is not None
    if not verify_password(current_password, user.password_hash):
        raise ValidationFailed("Current password is incorrect", code="wrong_password")
    if current_password == new_password:
        raise ValidationFailed("New password must be different from the current one")
    validate_password_strength(new_password)
    user.password_hash = hash_password(new_password, ctx.settings.bcrypt_rounds)
    user.must_change_password = False
    user.token_version += 1  # revoke older sessions
    audit.record(ctx, "update", "user_password", user.id, after={"must_change_password": False})
    return session_payload(ctx, user)


# ------------------------------------------------------------------ Admin: user management
def serialize_user(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "employee_id": user.employee.id if user.employee else None,
    }


def list_users(ctx: Ctx, role: str | None = None) -> list[dict[str, Any]]:
    require_roles(ctx, Role.ADMIN)
    stmt = select(User).order_by(User.role, User.full_name)
    if role:
        stmt = stmt.where(User.role == role)
    return [serialize_user(u) for u in ctx.db.scalars(stmt)]


def create_hr_user(ctx: Ctx, data: EmployeeInput) -> dict[str, Any]:
    """Admin creates an HR login. HR staff also get an employee profile (own check-in & leave)."""
    require_roles(ctx, Role.ADMIN)
    return create_employee_with_login(ctx, data, Role.HR, require_salary=False)


def _get_managed_user(ctx: Ctx, user_id: int) -> User:
    principal = require_roles(ctx, Role.ADMIN)
    user = ctx.db.get(User, user_id)
    if user is None:
        raise NotFound("User not found")
    if user.id == principal.user_id:
        raise Forbidden("You cannot change your own login here")
    if user.role == Role.ADMIN:
        raise Forbidden("Admin accounts cannot be managed from this screen")
    return user


def set_active(ctx: Ctx, user_id: int, active: bool) -> dict[str, Any]:
    user = _get_managed_user(ctx, user_id)
    if user.is_active == active:
        raise Conflict(f"User is already {'active' if active else 'inactive'}")
    before = {"is_active": user.is_active}
    user.is_active = active
    user.token_version += 1
    audit.record(ctx, "activate" if active else "deactivate", "user", user.id, before=before,
                 after={"is_active": active})
    return serialize_user(user)


def reset_password(ctx: Ctx, user_id: int) -> dict[str, Any]:
    user = _get_managed_user(ctx, user_id)
    temp = generate_temp_password()
    user.password_hash = hash_password(temp, ctx.settings.bcrypt_rounds)
    user.must_change_password = True
    user.token_version += 1
    audit.record(ctx, "reset_password", "user", user.id, after={"must_change_password": True})
    data = serialize_user(user)
    data["temporary_password"] = temp
    return data
