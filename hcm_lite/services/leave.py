"""M5 — Leave: apply / cancel, balances, overlap & holiday checks, approval inbox.

Rules (Section 3): working days only (skip weekends and holidays); quotas annual 14,
sick 8, casual 10, unpaid unlimited; overlap or over-balance = 422.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from ..context import Ctx, assert_can_view_employee, require_roles, require_self_employee
from ..errors import Conflict, Forbidden, NotFound, ValidationFailed
from ..models import Employee, LeaveRequest, LeaveType, RequestStatus, Role
from . import audit, comms
from .common import load_calendar, snapshot

ACTIVE_STATUSES = (RequestStatus.PENDING, RequestStatus.APPROVED)


# ------------------------------------------------------------------ lookups
def resolve_leave_type(ctx: Ctx, leave_type: str | int) -> LeaveType:
    if isinstance(leave_type, int) or (isinstance(leave_type, str) and leave_type.isdigit()):
        t = ctx.db.get(LeaveType, int(leave_type))
    else:
        key = str(leave_type).strip().lower()
        t = ctx.db.scalar(select(LeaveType).where(func.lower(LeaveType.code) == key))
        if t is None:
            t = ctx.db.scalar(select(LeaveType).where(func.lower(LeaveType.name).contains(key)))
    if t is None:
        raise ValidationFailed(f"Unknown leave type '{leave_type}'", details={"field": "leave_type"})
    return t


def approved_leave_on(ctx: Ctx, employee_id: int, day: date) -> LeaveRequest | None:
    leave = ctx.db.scalar(
        select(LeaveRequest)
        .options(joinedload(LeaveRequest.leave_type))
        .where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status == RequestStatus.APPROVED,
            LeaveRequest.start_date <= day,
            LeaveRequest.end_date >= day,
        )
    )
    if leave is None:
        return None
    return leave if load_calendar(ctx.db).is_working_day(day) else None


def approved_leaves_between(
    ctx: Ctx, start: date, end: date, *, employee_ids: Iterable[int] | None = None
) -> dict[int, dict[date, str]]:
    """{employee_id: {working_day: leave_type_code}} for approved leave overlapping [start, end]."""
    cal = load_calendar(ctx.db)
    stmt = (
        select(LeaveRequest.employee_id, LeaveRequest.start_date, LeaveRequest.end_date, LeaveType.code)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
        .where(LeaveRequest.status == RequestStatus.APPROVED, LeaveRequest.start_date <= end,
               LeaveRequest.end_date >= start)
    )
    if employee_ids is not None:
        ids = list(employee_ids)
        if not ids:
            return {}
        stmt = stmt.where(LeaveRequest.employee_id.in_(ids))
    out: dict[int, dict[date, str]] = defaultdict(dict)
    for emp_id, s, e, code in ctx.db.execute(stmt):
        for d in cal.working_days(max(s, start), min(e, end)):
            out[emp_id][d] = code
    return dict(out)


# ------------------------------------------------------------------ balances
def _used_days(ctx: Ctx, employee_id: int, year: int) -> dict[int, dict[str, int]]:
    rows = ctx.db.execute(
        select(LeaveRequest.leave_type_id, LeaveRequest.status, func.coalesce(func.sum(LeaveRequest.days), 0))
        .where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status.in_(ACTIVE_STATUSES),
            LeaveRequest.start_date >= date(year, 1, 1),
            LeaveRequest.start_date <= date(year, 12, 31),
        )
        .group_by(LeaveRequest.leave_type_id, LeaveRequest.status)
    ).all()
    used: dict[int, dict[str, int]] = defaultdict(lambda: {"approved": 0, "pending": 0})
    for type_id, status, total in rows:
        used[type_id][status] = int(total)
    return used


def balances(ctx: Ctx, employee_id: int | None = None, year: int | None = None) -> list[dict[str, Any]]:
    """Remaining = quota - approved days; available_to_apply also reserves pending requests."""
    principal = ctx.actor
    employee_id = employee_id or principal.employee_id
    if employee_id is None:
        raise ValidationFailed("No employee profile for this account")
    assert_can_view_employee(ctx, employee_id)
    year = year or ctx.today.year
    used = _used_days(ctx, employee_id, year)
    out = []
    for t in ctx.db.scalars(select(LeaveType).order_by(LeaveType.id)):
        u = used.get(t.id, {"approved": 0, "pending": 0})
        remaining = None if t.yearly_quota is None else t.yearly_quota - u["approved"]
        available = None if remaining is None else remaining - u["pending"]
        out.append({
            "leave_type": t.code,
            "name": t.name,
            "year": year,
            "quota": t.yearly_quota,
            "used": u["approved"],
            "pending": u["pending"],
            "remaining": remaining,
            "available_to_apply": available,
        })
    return out


# ------------------------------------------------------------------ validation shared by apply + smart leave
def validate_request(
    ctx: Ctx, employee_id: int, leave_type: LeaveType, start: date, end: date, *, exclude_id: int | None = None
) -> int:
    """Returns the number of working days or raises ValidationFailed (HTTP 422)."""
    if end < start:
        raise ValidationFailed("End date must be on or after start date", details={"field": "end"})
    if start.year != end.year:
        raise ValidationFailed("Leave cannot span two calendar years; please split it", details={"field": "end"})
    employee = ctx.db.get(Employee, employee_id)
    assert employee is not None
    if start < employee.join_date:
        raise ValidationFailed("Leave cannot start before the joining date", details={"field": "start"})
    days = load_calendar(ctx.db).count(start, end)
    if days == 0:
        raise ValidationFailed("The selected dates are weekends or public holidays — no working days",
                               code="no_working_days")
    overlap_stmt = select(LeaveRequest).where(
        LeaveRequest.employee_id == employee_id,
        LeaveRequest.status.in_(ACTIVE_STATUSES),
        LeaveRequest.start_date <= end,
        LeaveRequest.end_date >= start,
    )
    if exclude_id is not None:
        overlap_stmt = overlap_stmt.where(LeaveRequest.id != exclude_id)
    clash = ctx.db.scalar(overlap_stmt.limit(1))
    if clash is not None:
        raise ValidationFailed(
            f"Overlaps an existing {clash.status} leave ({clash.start_date} to {clash.end_date})",
            code="leave_overlap",
        )
    if leave_type.yearly_quota is not None:
        used = _used_days(ctx, employee_id, start.year).get(leave_type.id, {"approved": 0, "pending": 0})
        if exclude_id is not None:
            req = ctx.db.get(LeaveRequest, exclude_id)
            if req is not None and req.leave_type_id == leave_type.id:
                used[req.status] = used.get(req.status, 0) - req.days
        available = leave_type.yearly_quota - used["approved"] - used["pending"]
        if days > available:
            raise ValidationFailed(
                f"Not enough {leave_type.name.lower()} balance: requested {days} day(s), available {available}",
                code="insufficient_balance",
                details={"requested": days, "available": available},
            )
    return days


# ------------------------------------------------------------------ apply / cancel / decide
def serialize(r: LeaveRequest) -> dict[str, Any]:
    return {
        "id": r.id,
        "employee_id": r.employee_id,
        "employee_name": r.employee.full_name if r.employee else None,
        "leave_type": r.leave_type.code if r.leave_type else None,
        "start": r.start_date.isoformat(),
        "end": r.end_date.isoformat(),
        "days": r.days,
        "reason": r.reason,
        "status": r.status,
        "source": r.source,
        "review_comment": r.review_comment,
        "created_at": r.created_at.isoformat(),
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
    }


def apply_leave(
    ctx: Ctx, *, leave_type: str | int, start: date, end: date, reason: str, source: str = "form"
) -> dict[str, Any]:
    employee_id = require_self_employee(ctx)
    if not reason or len(reason.strip()) < 3:
        raise ValidationFailed("Please give a reason", details={"field": "reason"})
    t = resolve_leave_type(ctx, leave_type)
    days = validate_request(ctx, employee_id, t, start, end)
    req = LeaveRequest(
        employee_id=employee_id, leave_type_id=t.id, start_date=start, end_date=end, days=days,
        reason=reason.strip(), status=RequestStatus.PENDING, source=source if source in ("form", "smart") else "form",
        created_at=ctx.now,
    )
    ctx.db.add(req)
    ctx.db.flush()
    ctx.db.refresh(req)
    audit.record(ctx, "create", "leave_request", req.id, after=snapshot(req))
    comms.notify_role(ctx, Role.HR, "leave_request", "New leave request",
                      f"{ctx.actor.full_name} applied for {days} day(s) of {t.name.lower()} "
                      f"({start.isoformat()} to {end.isoformat()})", exclude_user_id=ctx.actor.user_id)
    return serialize(req)


def cancel_leave(ctx: Ctx, leave_id: int) -> dict[str, Any]:
    employee_id = require_self_employee(ctx)
    req = ctx.db.get(LeaveRequest, leave_id)
    if req is None or req.employee_id != employee_id:
        raise NotFound("Leave request not found")
    if req.status == RequestStatus.PENDING or (req.status == RequestStatus.APPROVED and req.start_date > ctx.today):
        before = snapshot(req)
        req.status = RequestStatus.CANCELLED
        req.decided_at = ctx.now
        audit.record(ctx, "cancel", "leave_request", req.id, before=before, after=snapshot(req))
        return serialize(req)
    raise Conflict(f"A {req.status} leave that has already started cannot be cancelled")


def decide_leave(ctx: Ctx, leave_id: int, *, approve: bool, comment: str | None = None) -> dict[str, Any]:
    principal = require_roles(ctx, Role.HR)
    req = ctx.db.get(LeaveRequest, leave_id)
    if req is None:
        raise NotFound("Leave request not found")
    if req.status != RequestStatus.PENDING:
        raise Conflict(f"Leave request is already {req.status}")
    if req.employee_id == principal.employee_id:
        raise Forbidden("You cannot approve your own leave request")
    if approve:  # re-validate against data that may have changed since the request
        validate_request(ctx, req.employee_id, req.leave_type, req.start_date, req.end_date, exclude_id=req.id)
    before = snapshot(req)
    req.status = RequestStatus.APPROVED if approve else RequestStatus.REJECTED
    req.reviewer_user_id, req.review_comment, req.decided_at = principal.user_id, comment, ctx.now
    audit.record(ctx, "approve" if approve else "reject", "leave_request", req.id, before=before, after=snapshot(req))
    comms.notify(
        ctx, req.employee.user_id, "leave_decision", f"Leave {req.status}",
        f"Your {req.leave_type.name.lower()} ({req.start_date} to {req.end_date}) was {req.status}"
        + (f": {comment}" if comment else ""),
    )
    return serialize(req)


def list_leaves(
    ctx: Ctx,
    *,
    status: str | None = None,
    employee_id: int | None = None,
    department_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    principal = ctx.actor
    stmt = select(LeaveRequest).options(joinedload(LeaveRequest.leave_type), joinedload(LeaveRequest.employee))
    if employee_id:
        assert_can_view_employee(ctx, employee_id)
        stmt = stmt.where(LeaveRequest.employee_id == employee_id)
    if not principal.sees_all_employees:
        stmt = stmt.where(LeaveRequest.employee_id == (principal.employee_id or -1))
    if department_id:
        stmt = stmt.join(Employee, Employee.id == LeaveRequest.employee_id).where(Employee.department_id == department_id)
    if status:
        stmt = stmt.where(LeaveRequest.status == status)
    if date_from:
        stmt = stmt.where(LeaveRequest.end_date >= date_from)
    if date_to:
        stmt = stmt.where(LeaveRequest.start_date <= date_to)
    rows = ctx.db.scalars(stmt.order_by(LeaveRequest.start_date.desc(), LeaveRequest.id.desc()).limit(limit)).unique().all()
    return {"items": [serialize(r) for r in rows], "total": len(rows)}


def who_is_on_leave(ctx: Ctx, start: date, end: date, department_id: int | None = None) -> dict[str, Any]:
    """Approved leave overlapping a range, grouped by type. Employees only see themselves."""
    principal = ctx.actor
    stmt = select(Employee)
    if not principal.sees_all_employees:
        stmt = stmt.where(Employee.id == (principal.employee_id or -1))
    if department_id:
        stmt = stmt.where(Employee.department_id == department_id)
    employees = {e.id: e for e in ctx.db.scalars(stmt)}
    by_emp = approved_leaves_between(ctx, start, end, employee_ids=list(employees))
    people, by_type = [], defaultdict(int)
    for emp_id, days in by_emp.items():
        codes = sorted(set(days.values()))
        for code in codes:
            by_type[code] += 1
        people.append({"employee_id": emp_id, "name": employees[emp_id].full_name,
                       "leave_types": codes, "days_in_range": len(days)})
    people.sort(key=lambda p: p["name"])
    return {"from": start.isoformat(), "to": end.isoformat(), "count": len(people),
            "by_type": dict(by_type), "employees": people}


def working_days_preview(ctx: Ctx, start: date, end: date) -> int:
    return load_calendar(ctx.db).count(start, end)


def next_weekday(d: date, weekday: int) -> date:
    return d + timedelta(days=(weekday - d.weekday()) % 7)
