"""M6 — Salary structure: Gross = 1.5 x basic  (HRA 40% + medical 10% of basic)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from ..context import Ctx, require_roles
from ..errors import NotFound, ValidationFailed
from ..models import Employee, Role, SalaryStructure
from ..db import to_money
from . import audit
from .common import snapshot

HRA_RATE = Decimal("0.40")
MEDICAL_RATE = Decimal("0.10")


def compute_structure(basic: Decimal | float | int) -> dict[str, Decimal]:
    basic_d = to_money(basic)
    if basic_d <= 0:
        raise ValidationFailed("Basic salary must be greater than 0")
    hra = to_money(basic_d * HRA_RATE)
    medical = to_money(basic_d * MEDICAL_RATE)
    return {"basic": basic_d, "hra": hra, "medical": medical, "gross": to_money(basic_d + hra + medical)}


def current_structure(ctx: Ctx, employee_id: int, as_of: date | None = None) -> SalaryStructure | None:
    stmt = select(SalaryStructure).where(SalaryStructure.employee_id == employee_id)
    if as_of is not None:
        stmt = stmt.where(SalaryStructure.effective_from <= as_of)
    return ctx.db.scalar(stmt.order_by(SalaryStructure.effective_from.desc(), SalaryStructure.id.desc()).limit(1))


def serialize(s: SalaryStructure | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {
        "basic": float(s.basic),
        "hra": float(s.hra),
        "medical": float(s.medical),
        "gross": float(s.gross),
        "effective_from": s.effective_from.isoformat(),
    }


def add_structure(ctx: Ctx, employee: Employee, basic: Decimal | float, effective_from: date) -> SalaryStructure:
    parts = compute_structure(basic)
    s = SalaryStructure(employee_id=employee.id, effective_from=effective_from, created_at=ctx.now, **parts)
    ctx.db.add(s)
    ctx.db.flush()
    return s


def set_salary(ctx: Ctx, employee_id: int, basic: float, effective_from: date | None = None) -> dict[str, Any]:
    """HR manages salary structures (Admin views). Employees never edit their own salary."""
    require_roles(ctx, Role.HR)
    employee = ctx.db.get(Employee, employee_id)
    if employee is None:
        raise NotFound("Employee not found")
    before = serialize(current_structure(ctx, employee_id))
    s = add_structure(ctx, employee, basic, effective_from or ctx.today)
    audit.record(ctx, "update", "salary_structure", employee_id, before=before, after=snapshot(s))
    return serialize(s) or {}


def get_salary(ctx: Ctx, employee_id: int) -> dict[str, Any] | None:
    require_roles(ctx, Role.ADMIN, Role.HR)
    if ctx.db.get(Employee, employee_id) is None:
        raise NotFound("Employee not found")
    return serialize(current_structure(ctx, employee_id))
