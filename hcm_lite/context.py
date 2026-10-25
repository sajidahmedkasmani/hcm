"""Request context passed into every service call: DB session, acting principal, clock, settings.

RBAC lives here as small, explicit guard functions so every service applies the same checks
whether it is called from a REST route or from an AI tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from .clock import Clock
from .config import Settings
from .errors import Forbidden
from .models import Role


@dataclass(frozen=True)
class Principal:
    user_id: int
    role: Role
    email: str
    full_name: str
    employee_id: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_hr(self) -> bool:
        return self.role == Role.HR

    @property
    def is_employee(self) -> bool:
        return self.role == Role.EMPLOYEE

    @property
    def sees_all_employees(self) -> bool:
        return self.role in (Role.ADMIN, Role.HR)


SYSTEM_PRINCIPAL = Principal(user_id=0, role=Role.ADMIN, email="system", full_name="System")


@dataclass
class Ctx:
    db: Session
    clock: Clock
    settings: Settings
    principal: Principal | None = None
    token_claims: dict | None = None

    @property
    def now(self) -> datetime:
        return self.clock.now()

    @property
    def today(self) -> date:
        return self.clock.today()

    @property
    def actor(self) -> Principal:
        if self.principal is None:
            raise Forbidden("Authentication required")
        return self.principal

    def with_principal(self, principal: Principal, token_claims: dict | None = None) -> "Ctx":
        return Ctx(db=self.db, clock=self.clock, settings=self.settings, principal=principal,
                   token_claims=token_claims)


# ------------------------------------------------------------------ guards
def require_roles(ctx: Ctx, *roles: Role) -> Principal:
    principal = ctx.actor
    if principal.role not in roles:
        allowed = ", ".join(r.value for r in roles)
        raise Forbidden(f"This action requires role: {allowed}")
    return principal


def require_authenticated(ctx: Ctx) -> Principal:
    """Any signed-in role may proceed."""
    return ctx.actor


def require_self_employee(ctx: Ctx) -> int:
    """For self-service actions (check-in, leave): the caller must have an employee profile."""
    principal = ctx.actor
    if principal.employee_id is None:
        raise Forbidden("This action is only available to users with an employee profile")
    return principal.employee_id


def assert_can_view_employee(ctx: Ctx, employee_id: int) -> None:
    """Data-scope guard: an Employee only reads rows with their own employee_id."""
    principal = ctx.actor
    if principal.sees_all_employees:
        return
    if principal.employee_id != employee_id:
        raise Forbidden("You can only access your own records")
