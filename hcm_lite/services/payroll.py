"""M6 — Payroll Lite: monthly run (HR drafts, Admin finalizes & locks), tax slab, payslips.

Rules (Section 3):
  Gross = 1.5 x basic (HRA 40%, medical 10%).
  Per day = gross / working days; deduct absent + unpaid + 0.5 x half days; prorate joiners/leavers.
  Tax slab on gross x 12 (illustrative): 0 to 600k; 5% to 1.2M; 30k + 15% to 2.4M;
  210k + 25% to 3.6M; 510k + 35% above. Finalized runs are locked.
Bonus: late penalty rule (every 3 lates = 0.5 day) when enabled in company settings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from ..context import Ctx, assert_can_view_employee, require_roles
from ..db import to_money
from ..errors import Conflict, NotFound, ValidationFailed
from ..models import (
    AttendanceRecord,
    Employee,
    LeaveType,
    PayrollRun,
    PayrollStatus,
    Payslip,
    Role,
)
from ..models import (
    AttendanceStatus as S,
)
from . import audit, comms
from .attendance import effective_status
from .common import get_company, load_calendar, month_bounds
from .leave import approved_leaves_between
from .salary import current_structure

# (upper bound of slab, fixed amount at slab start, marginal rate, slab start)
TAX_SLABS: tuple[tuple[Decimal | None, Decimal, Decimal, Decimal], ...] = (
    (Decimal("600000"), Decimal("0"), Decimal("0"), Decimal("0")),
    (Decimal("1200000"), Decimal("0"), Decimal("0.05"), Decimal("600000")),
    (Decimal("2400000"), Decimal("30000"), Decimal("0.15"), Decimal("1200000")),
    (Decimal("3600000"), Decimal("210000"), Decimal("0.25"), Decimal("2400000")),
    (None, Decimal("510000"), Decimal("0.35"), Decimal("3600000")),
)


# ------------------------------------------------------------------ pure calculation (unit tested)
def annual_tax(annual_income: Decimal | float | int) -> Decimal:
    income = Decimal(str(annual_income))
    for upper, fixed, rate, start in TAX_SLABS:
        if upper is None or income <= upper:
            return to_money(fixed + max(income - start, Decimal("0")) * rate)
    raise AssertionError("unreachable")  # pragma: no cover


@dataclass(frozen=True)
class AttendanceFacts:
    absent_days: float = 0.0
    unpaid_leave_days: float = 0.0
    half_days: float = 0.0
    late_count: int = 0


@dataclass(frozen=True)
class PayslipCalc:
    basic: Decimal
    hra: Decimal
    medical: Decimal
    gross: Decimal
    working_days: int
    eligible_days: int
    per_day: Decimal
    prorated_gross: Decimal
    absent_days: float
    unpaid_leave_days: float
    half_days: float
    late_count: int
    late_penalty_days: float
    deduction_days: float
    deductions: Decimal
    taxable_annual: Decimal
    tax: Decimal
    net: Decimal


def calculate_payslip(
    *,
    basic: Decimal,
    hra: Decimal,
    medical: Decimal,
    gross: Decimal,
    working_days: int,
    eligible_days: int,
    facts: AttendanceFacts,
    late_penalty_enabled: bool = False,
) -> PayslipCalc:
    if working_days <= 0:
        raise ValidationFailed("Month has no working days")
    exact_per_day = gross / Decimal(working_days)
    prorated = gross if eligible_days >= working_days else to_money(exact_per_day * eligible_days)
    late_penalty_days = (facts.late_count // 3) * 0.5 if late_penalty_enabled else 0.0
    deduction_days = facts.absent_days + facts.unpaid_leave_days + 0.5 * facts.half_days + late_penalty_days
    deductions = min(to_money(exact_per_day * Decimal(str(deduction_days))), prorated)
    taxable_annual = to_money(prorated * 12)
    # tax per the slab on gross x 12, but never more than what is left after deductions (net >= 0)
    tax = min(to_money(annual_tax(taxable_annual) / 12), prorated - deductions)
    net = to_money(prorated - deductions - tax)
    return PayslipCalc(
        basic=basic, hra=hra, medical=medical, gross=gross, working_days=working_days,
        eligible_days=eligible_days, per_day=exact_per_day.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        prorated_gross=prorated, absent_days=facts.absent_days, unpaid_leave_days=facts.unpaid_leave_days,
        half_days=facts.half_days, late_count=facts.late_count, late_penalty_days=late_penalty_days,
        deduction_days=deduction_days, deductions=deductions, taxable_annual=taxable_annual, tax=tax, net=net,
    )


# ------------------------------------------------------------------ facts from the database
def attendance_facts(ctx: Ctx, employee: Employee, window_start: date, window_end: date) -> tuple[int, AttendanceFacts]:
    """Eligible working days and attendance facts for an employee within a payroll window."""
    cal = load_calendar(ctx.db)
    working = cal.working_days(window_start, window_end)
    as_of = min(window_end, ctx.today - timedelta(days=1))  # today is still in progress; future days aren't absences
    records = {
        r.work_date: r
        for r in ctx.db.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.employee_id == employee.id,
                AttendanceRecord.work_date >= window_start,
                AttendanceRecord.work_date <= window_end,
            )
        )
    }
    leaves = approved_leaves_between(ctx, window_start, window_end, employee_ids=[employee.id]).get(employee.id, {})
    unpaid_codes = set(ctx.db.scalars(select(LeaveType.code).where(LeaveType.is_paid.is_(False))))
    tracked_from = cal.tracking_start(employee.join_date, employee.created_at)
    absent = unpaid = half = late = 0
    for d in working:
        rec = records.get(d)
        if rec is not None:
            if effective_status(rec, ctx.today) == S.HALF_DAY:
                half += 1
            if rec.is_late:
                late += 1
        elif d in leaves:
            if leaves[d] in unpaid_codes:
                unpaid += 1
        elif tracked_from <= d <= as_of:
            absent += 1
    return len(working), AttendanceFacts(float(absent), float(unpaid), float(half), late)


# ------------------------------------------------------------------ serialization
def serialize_payslip(p: Payslip, *, with_employee: bool = True) -> dict[str, Any]:
    data = {
        "id": p.id,
        "run_id": p.run_id,
        "month": p.run.month if p.run else None,
        "status": p.run.status if p.run else None,
        "employee_id": p.employee_id,
        "basic": float(p.basic), "hra": float(p.hra), "medical": float(p.medical), "gross": float(p.gross),
        "working_days": p.working_days, "eligible_days": p.eligible_days, "per_day": float(p.per_day),
        "prorated_gross": float(p.prorated_gross), "absent_days": p.absent_days,
        "unpaid_leave_days": p.unpaid_leave_days, "half_days": p.half_days, "late_count": p.late_count,
        "late_penalty_days": p.late_penalty_days, "deduction_days": p.deduction_days,
        "deductions": float(p.deductions), "taxable_annual": float(p.taxable_annual),
        "tax": float(p.tax), "net": float(p.net),
    }
    if with_employee and p.employee is not None:
        data["employee_name"] = p.employee.full_name
        data["employee_code"] = p.employee.employee_code
    return data


def serialize_run(run: PayrollRun, *, include_lines: bool = True) -> dict[str, Any]:
    lines = sorted(run.payslips, key=lambda p: p.employee.full_name if p.employee else "")
    totals = {
        "employees": len(lines),
        "gross": float(sum((p.prorated_gross for p in lines), Decimal("0"))),
        "deductions": float(sum((p.deductions for p in lines), Decimal("0"))),
        "tax": float(sum((p.tax for p in lines), Decimal("0"))),
        "net": float(sum((p.net for p in lines), Decimal("0"))),
    }
    data = {
        "id": run.id, "month": run.month, "status": run.status, "working_days": run.working_days,
        "created_at": run.created_at.isoformat(), "refreshed_at": run.refreshed_at.isoformat(),
        "finalized_at": run.finalized_at.isoformat() if run.finalized_at else None, "totals": totals,
    }
    if include_lines:
        data["payslips"] = [serialize_payslip(p) for p in lines]
    return data


# ------------------------------------------------------------------ run lifecycle
def _get_run(ctx: Ctx, run_id: int) -> PayrollRun:
    run = ctx.db.scalar(
        select(PayrollRun).options(joinedload(PayrollRun.payslips).joinedload(Payslip.employee))
        .where(PayrollRun.id == run_id)
    )
    if run is None:
        raise NotFound("Payroll run not found")
    return run


def create_or_refresh_draft(ctx: Ctx, month: str) -> dict[str, Any]:
    """HR creates (or recalculates) the monthly draft. Finalized months are locked (409)."""
    require_roles(ctx, Role.HR)
    start, end = month_bounds(month)
    if start > ctx.today:
        raise ValidationFailed("Cannot run payroll for a future month")
    cal = load_calendar(ctx.db)
    working_days = cal.count(start, end)
    company = get_company(ctx.db)

    run = ctx.db.scalar(select(PayrollRun).where(PayrollRun.month == month))
    action = "create"
    if run is not None:
        if run.status == PayrollStatus.FINALIZED:
            raise Conflict(f"Payroll for {month} is finalized and locked", code="payroll_locked")
        action = "update"
        run.payslips.clear()
        ctx.db.flush()
        run.refreshed_at = ctx.now
        run.working_days = working_days
    else:
        run = PayrollRun(month=month, status=PayrollStatus.DRAFT, working_days=working_days,
                         created_by_user_id=ctx.actor.user_id, created_at=ctx.now, refreshed_at=ctx.now)
        ctx.db.add(run)
        ctx.db.flush()

    employees = ctx.db.scalars(
        select(Employee).where(Employee.join_date <= end)
        .where((Employee.exit_date.is_(None)) | (Employee.exit_date >= start))
        .order_by(Employee.full_name)
    ).all()
    skipped = []
    for e in employees:
        structure = current_structure(ctx, e.id, as_of=end)
        if structure is None:
            skipped.append({"employee_id": e.id, "name": e.full_name, "reason": "no salary structure"})
            continue
        window_start = max(start, e.join_date)
        window_end = min(end, e.exit_date) if e.exit_date else end
        eligible, facts = attendance_facts(ctx, e, window_start, window_end)
        calc = calculate_payslip(
            basic=structure.basic, hra=structure.hra, medical=structure.medical, gross=structure.gross,
            working_days=working_days, eligible_days=eligible, facts=facts,
            late_penalty_enabled=company.late_penalty_enabled,
        )
        run.payslips.append(Payslip(employee_id=e.id, department_id=e.department_id, **asdict(calc)))
    ctx.db.flush()
    ctx.db.refresh(run)
    data = serialize_run(_get_run(ctx, run.id))
    data["skipped"] = skipped
    audit.record(ctx, action, "payroll_run", run.id, after={"month": month, "status": run.status, **data["totals"]})
    return data


def finalize_run(ctx: Ctx, run_id: int) -> dict[str, Any]:
    """Only Admin can lock a month. After this the run and its payslips are read-only."""
    principal = require_roles(ctx, Role.ADMIN)
    run = _get_run(ctx, run_id)
    if run.status == PayrollStatus.FINALIZED:
        raise Conflict(f"Payroll for {run.month} is already finalized", code="payroll_locked")
    if not run.payslips:
        raise ValidationFailed("Cannot finalize an empty payroll run")
    before = {"status": run.status}
    run.status = PayrollStatus.FINALIZED
    run.finalized_by_user_id = principal.user_id
    run.finalized_at = ctx.now
    for p in run.payslips:
        comms.notify(ctx, p.employee.user_id, "payslip", f"Payslip for {run.month} is ready",
                     f"Net pay PKR {p.net:,.2f}. Download it from Payslips.")
    audit.record(ctx, "finalize", "payroll_run", run.id, before=before,
                 after={"status": run.status, "month": run.month})
    return serialize_run(run)


def list_runs(ctx: Ctx) -> list[dict[str, Any]]:
    require_roles(ctx, Role.ADMIN, Role.HR)
    runs = ctx.db.scalars(
        select(PayrollRun).options(joinedload(PayrollRun.payslips).joinedload(Payslip.employee))
        .order_by(PayrollRun.month.desc())
    ).unique().all()
    return [serialize_run(r, include_lines=False) for r in runs]


def get_run(ctx: Ctx, run_id: int) -> dict[str, Any]:
    require_roles(ctx, Role.ADMIN, Role.HR)
    return serialize_run(_get_run(ctx, run_id))


# ------------------------------------------------------------------ payslips
def list_payslips(
    ctx: Ctx, *, employee_id: int | None = None, month: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Employees see only their own *finalized* payslips; HR/Admin see all (drafts included)."""
    principal = ctx.actor
    stmt = (
        select(Payslip).join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .options(joinedload(Payslip.run), joinedload(Payslip.employee))
    )
    if employee_id:
        assert_can_view_employee(ctx, employee_id)
        stmt = stmt.where(Payslip.employee_id == employee_id)
    if not principal.sees_all_employees:
        stmt = stmt.where(Payslip.employee_id == (principal.employee_id or -1),
                          PayrollRun.status == PayrollStatus.FINALIZED)
    if month:
        stmt = stmt.where(PayrollRun.month == month)
    rows = ctx.db.scalars(stmt.order_by(PayrollRun.month.desc()).limit(limit)).unique().all()
    return [serialize_payslip(p) for p in rows]


def get_payslip_obj(ctx: Ctx, payslip_id: int) -> Payslip:
    p = ctx.db.scalar(
        select(Payslip).options(joinedload(Payslip.run), joinedload(Payslip.employee)).where(Payslip.id == payslip_id)
    )
    if p is None:
        raise NotFound("Payslip not found")
    assert_can_view_employee(ctx, p.employee_id)
    if not ctx.actor.sees_all_employees and p.run.status != PayrollStatus.FINALIZED:
        raise NotFound("Payslip not found")
    return p


def payslip_pdf(ctx: Ctx, payslip_id: int) -> tuple[str, bytes]:
    from .payslip_pdf import render_payslip_pdf

    p = get_payslip_obj(ctx, payslip_id)
    company = get_company(ctx.db)
    filename = f"payslip_{p.employee.employee_code}_{p.run.month}.pdf"
    return filename, render_payslip_pdf(company.name, p)
