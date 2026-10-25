"""Dependency wiring: app state, DB session per request, authenticated context, commit helper."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, TypeVar

from fastapi import Depends, Header, Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..ai.service import AIService
from ..clock import Clock
from ..config import Settings
from ..context import Ctx
from ..errors import Forbidden, Unauthorized
from ..services import auth

T = TypeVar("T")


@dataclass
class AppState:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker
    clock: Clock
    ai: AIService


def get_state(request: Request) -> AppState:
    return request.app.state.hcm


def get_db(state: AppState = Depends(get_state)) -> Iterator[Session]:
    session: Session = state.session_factory()
    try:
        yield session
    finally:
        session.rollback()  # no-op after an explicit commit; discards partial work on errors
        session.close()


def public_ctx(state: AppState = Depends(get_state), db: Session = Depends(get_db)) -> Ctx:
    return Ctx(db=db, clock=state.clock, settings=state.settings)


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def ctx_allow_password_change(
    ctx: Ctx = Depends(public_ctx), authorization: str | None = Header(default=None)
) -> Ctx:
    principal, _user, claims = auth.principal_from_token(ctx, _bearer(authorization))
    return ctx.with_principal(principal, claims)


def current_ctx(ctx: Ctx = Depends(public_ctx), authorization: str | None = Header(default=None)) -> Ctx:
    """Authenticated context. Users with a temporary password must change it before anything else."""
    principal, user, claims = auth.principal_from_token(ctx, _bearer(authorization))
    if user.must_change_password:
        raise Forbidden("Please change your temporary password first", code="password_change_required")
    return ctx.with_principal(principal, claims)


def commit(ctx: Ctx, result: T) -> T:
    """Unit of work: commit once per request, after the service succeeded."""
    ctx.db.commit()
    return result


def ai_service(state: AppState = Depends(get_state)) -> AIService:
    return state.ai


Json = dict[str, Any]
