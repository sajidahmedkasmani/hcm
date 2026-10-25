"""M7 — Dashboards & analytics. All numbers are computed on the server with database
aggregation (GROUP BY) plus working-day calendar arithmetic, filterable by date range
and department. The AI tools call these same functions, so answers match the dashboards.

Metric formulas (Section 7):
  Headcount       = active + probation employees on the end date
  Attrition %     = exits in period / average headcount x 100
  Attendance rate = (present + late + missing_checkout + 0.5 x half_day) / (working-day records - on_leave)
  Late %          = late arrivals / days checked in
  Absenteeism %   = absent days / expected working days
  Leave util. %   = approved days / quota  (per leave type and department)
  Payroll cost    = sum of gross and net by month and department; avg salary = mean gross
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select

from ..context import Ctx, require_roles
from ..errors import ValidationFailed
from ..models import (
    AttendanceCorrection,
    AttendanceRecord,
    AttendanceStatus as S,
    AuditLog,
    Department,
    Designation,
    Employee,
    LeaveRequest,
    LeaveType,
    PayrollRun,
    PayrollStatus,
    Payslip,
    RequestStatus,
    Role,
    SalaryStructure,
)
from ..db import Money
from .attendance import effective_status_expr
from .common import WorkCalendar, load_calendar, month_key, resolve_range
from .leave import approved_leaves_between

ATTENDED = (S.PRESENT, S.LATE, S.HALF_DAY, S.MISSING_CHECKOUT)


def _pct(num: float, den: float) -> float:
    return round(num / den * 100, 1) if den else 0.0


# ------------------------------------------------------------------ scope helpers
def _scope(ctx: Ctx, department_id: int | None, employee_id: int | None) -> tuple[int | None, int | None]:
    """Employees are always narrowed to themselves; HR/Admin may filter freely."""
    principal = ctx.actor
    if principal.sees_all_employees:
        return department_id, employee_id
    if principal.employee_id is None:
        raise ValidationFailed("No employee profile for this account")
    return None, principal.employee_id


def _employees_in_scope(ctx: Ctx, start: date, end: date, department_id: int | None, employee_id: int | None) -> list[Employee]:
    stmt = select(Employee).where(
        Employee.join_date <= end, or_(Employee.exit_date.is_(None), Employee.exit_date >= start)
    )
    if department_id:
        stmt = stmt.where(Employee.department_id == department_id)
    if employee_id:
        stmt = stmt.where(Employee.id == employee_id)
    return list(ctx.db.scalars(stmt))


# ------------------------------------------------------------------ headcount & attrition
def _employed_on_clause(d: date):
    return and_(Employee.join_date <= d, or_(Employee.exit_date.is_(None), Employee.exit_date >= d))


def headcount_on(ctx: Ctx, d: date, department_id: int | None = None) -> int:
    stmt = select(func.count(Employee.id)).where(_employed_on_clause(d))
    if department_id:
        stmt = stmt.where(Employee.department_id == department_id)
    return int(ctx.db.scalar(stmt) or 0)


def headcount_by(ctx: Ctx, d: date, dimension: str = "department", department_id: int | None = None) -> list[dict[str, Any]]:
    if dimension == "department":
        label = Department.name
        stmt = select(label, func.count(Employee.id)).join(Department, Department.id == Employee.department_id)
    elif dimension == "designation":
        label = func.coalesce(Designation.name, "Unassigned")
        stmt = select(label, func.count(Employee.id)).outerjoin(Designation, Designation.id == Employee.designation_id)
    elif dimension == "gender":
        label = func.coalesce(Employee.gender, "unspecified")
        stmt = select(label, func.count(Employee.id))
    else:
        raise ValidationFailed(f"Unknown dimension {dimension}")
    stmt = stmt.where(_employed_on_clause(d))
    if department_id:
        stmt = stmt.where(Employee.department_id == department_id)
    rows = ctx.db.execute(stmt.group_by(label).order_by(func.count(Employee.id).desc())).all()
    return [{"label": name, "count": int(n)} for name, n in rows]


def attrition(ctx: Ctx, start: date, end: date, department_id: int | None = None) -> dict[str, Any]:
    exits_stmt = select(func.count(Employee.id)).where(Employee.exit_date >= start, Employee.exit_date <= end)
    if department_id:
        exits_stmt = exits_stmt.where(Employee.department_id == department_id)
    exits = int(ctx.db.scalar(exits_stmt) or 0)
    avg_headcount = (headcount_on(ctx, start, department_id) + headcount_on(ctx, end, department_id)) / 2
    return {"from": start.isoformat(), "to": end.isoformat(), "exits": exits,
            "average_headcount": avg_headcount, "attrition_pct": _pct(exits, avg_headcount)}


def tenure_buckets(ctx: Ctx, d: date, department_id: int | None = None) -> list[dict[str, Any]]:
    stmt = select(Employee.join_date).where(_employed_on_clause(d))
    if department_id:
        stmt = stmt.where(Employee.department_id == department_id)
    buckets = Counter()
    for (join,) in ctx.db.execute(stmt):
        months = (d.year - join.year) * 12 + (d.month - join.month)
        key = "< 6 months" if months < 6 else "6-12 months" if months < 12 else "1-3 years" if months < 36 else "3+ years"
        buckets[key] += 1
    order = ["< 6 months", "6-12 months", "1-3 years", "3+ years"]
    return [{"label": k, "count": buckets.get(k, 0)} for k in order]


# ------------------------------------------------------------------ attendance core
def _status_counts_sql(ctx: Ctx, start: date, end: date, employee_ids: list[int], *, by_day: bool = False):
    """GROUP BY (employee | day, effective status) with late-arrival sums. Holidays are excluded in SQL;
    check-in on weekends is blocked, so every record is on a working day."""
    from ..models import Holiday

    eff = effective_status_expr(ctx.today)
    key = AttendanceRecord.work_date if by_day else AttendanceRecord.employee_id
    stmt = (
        select(key, eff.label("st"), func.count(AttendanceRecord.id),
               func.sum(case((AttendanceRecord.is_late.is_(True), 1), else_=0)))
        .where(AttendanceRecord.work_date >= start, AttendanceRecord.work_date <= end,
               AttendanceRecord.employee_id.in_(employee_ids),
               AttendanceRecord.work_date.not_in(select(Holiday.day)))
        .group_by(key, eff)
    )
    return ctx.db.execute(stmt).all()


def _expected_days(cal: WorkCalendar, e: Employee, start: date, end: date, today: date) -> list[date]:
    """Past working days in range while employed (today is handled separately by the caller)."""
    last_past = min(end, today - timedelta(days=1), e.exit_date or end)
    return cal.working_days(max(start, cal.tracking_start(e.join_date, e.created_at)), last_past)


def per_employee_attendance(
    ctx: Ctx, start: date, end: date, *, department_id: int | None = None, employee_id: int | None = None
) -> dict[int, dict[str, Any]]:
    today = ctx.today
    cal = load_calendar(ctx.db)
    employees = _employees_in_scope(ctx, start, end, department_id, employee_id)
    ids = [e.id for e in employees]
    if not ids:
        return {}
    counts: dict[int, Counter] = defaultdict(Counter)
    for emp_id, status, n, lates in _status_counts_sql(ctx, start, min(end, today), ids):
        counts[emp_id][status] += int(n)
        counts[emp_id]["late_arrivals"] += int(lates or 0)
    leaves = approved_leaves_between(ctx, start, min(end, today), employee_ids=ids)
    has_record_today = set()
    if start <= today <= end:
        has_record_today = set(ctx.db.scalars(
            select(AttendanceRecord.employee_id).where(AttendanceRecord.work_date == today,
                                                       AttendanceRecord.employee_id.in_(ids))
        ))
    out: dict[int, dict[str, Any]] = {}
    for e in employees:
        past_days = _expected_days(cal, e, start, end, today)
        expected = len(past_days)
        leave_days = leaves.get(e.id, {})
        on_leave = sum(1 for d in past_days if d in leave_days)
        employed_today = e.join_date <= today and (e.exit_date is None or today <= e.exit_date)
        if start <= today <= end and employed_today and cal.is_working_day(today):
            if e.id in has_record_today or today in leave_days:  # today counts only once it has an outcome
                expected += 1
                on_leave += 1 if today in leave_days and e.id not in has_record_today else 0
        c = counts.get(e.id, Counter())
        attended = sum(c[s] for s in ATTENDED)
        absent = max(expected - attended - on_leave, 0)
        out[e.id] = {
            "employee_id": e.id, "department_id": e.department_id, "expected": expected,
            "present": c[S.PRESENT], "late": c[S.LATE], "half_day": c[S.HALF_DAY],
            "missing_checkout": c[S.MISSING_CHECKOUT], "on_leave": on_leave, "absent": absent,
            "late_arrivals": c["late_arrivals"], "attended": attended,
        }
    return out


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ("expected", "present", "late", "half_day", "missing_checkout", "on_leave", "absent", "late_arrivals",
            "attended")
    t = {k: sum(r[k] for r in rows) for k in keys}
    numerator = t["present"] + t["late"] + t["missing_checkout"] + 0.5 * t["half_day"]
    t["attendance_rate_pct"] = _pct(numerator, t["expected"] - t["on_leave"])
    t["late_pct"] = _pct(t["late_arrivals"], t["attended"])
    t["absenteeism_pct"] = _pct(t["absent"], t["expected"])
    t["employees"] = len(rows)
    return t


def attendance_stats(
    ctx: Ctx, *, date_from: date | None = None, date_to: date | None = None, period: str | None = None,
    department_id: int | None = None, employee_id: int | None = None,
) -> dict[str, Any]:
    department_id, employee_id = _scope(ctx, department_id, employee_id)
    start, end = resolve_range(ctx.today, date_from, date_to, period)
    rows = list(per_employee_attendance(ctx, start, end, department_id=department_id, employee_id=employee_id).values())
    return {"from": start.isoformat(), "to": end.isoformat(), **_summarize(rows)}


def attendance_by_department(ctx: Ctx, start: date, end: date, department_id: int | None = None) -> list[dict[str, Any]]:
    per_emp = per_employee_attendance(ctx, start, end, department_id=department_id)
    names = dict(ctx.db.execute(select(Department.id, Department.name)).all())
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in per_emp.values():
        grouped[row["department_id"]].append(row)
    out = [{"department": names.get(dept), "department_id": dept, **_summarize(rows)} for dept, rows in grouped.items()]
    return sorted(out, key=lambda r: r["absenteeism_pct"], reverse=True)


def attendance_trend(ctx: Ctx, start: date, end: date, department_id: int | None = None) -> list[dict[str, Any]]:
    today = ctx.today
    cal = load_calendar(ctx.db)
    employees = _employees_in_scope(ctx, start, end, department_id, None)
    ids = [e.id for e in employees]
    if not ids:
        return []
    day_counts: dict[date, Counter] = defaultdict(Counter)
    for day, status, n, _lates in _status_counts_sql(ctx, start, min(end, today), ids, by_day=True):
        day_counts[day][status] += int(n)
    leaves = approved_leaves_between(ctx, start, min(end, today), employee_ids=ids)
    trend = []
    starts = {e.id: cal.tracking_start(e.join_date, e.created_at) for e in employees}
    for d in cal.working_days(start, min(end, today)):
        c = day_counts.get(d, Counter())
        attended = sum(c[s] for s in ATTENDED)
        on_leave = sum(1 for e in employees if d in leaves.get(e.id, {}))
        if d == today:
            expected = attended + on_leave
        else:
            expected = sum(1 for e in employees if starts[e.id] <= d and (e.exit_date is None or d <= e.exit_date))
            expected = max(expected, attended + on_leave)
        numerator = c[S.PRESENT] + c[S.LATE] + c[S.MISSING_CHECKOUT] + 0.5 * c[S.HALF_DAY]
        trend.append({
            "date": d.isoformat(), "attended": attended, "on_leave": on_leave,
            "absent": max(expected - attended - on_leave, 0), "late": c[S.LATE],
            "attendance_rate_pct": _pct(numerator, expected - on_leave),
        })
    return trend


def top_late_comers(
    ctx: Ctx, *, date_from: date | None = None, date_to: date | None = None, period: str | None = None,
    department_id: int | None = None, limit: int = 5,
) -> dict[str, Any]:
    require_roles(ctx, Role.ADMIN, Role.HR)
    start, end = resolve_range(ctx.today, date_from, date_to, period)
    lates = func.sum(case((AttendanceRecord.is_late.is_(True), 1), else_=0))
    stmt = (
        select(Employee.id, Employee.full_name, Department.name, lates.label("lates"), func.count(AttendanceRecord.id))
        .join(Employee, Employee.id == AttendanceRecord.employee_id)
        .join(Department, Department.id == Employee.department_id)
        .where(AttendanceRecord.work_date >= start, AttendanceRecord.work_date <= end)
        .group_by(Employee.id, Employee.full_name, Department.name)
        .having(lates > 0)
        .order_by(lates.desc(), Employee.full_name)
        .limit(max(1, min(limit, 50)))
    )
    if department_id:
        stmt = stmt.where(Employee.department_id == department_id)
    rows = ctx.db.execute(stmt).all()
    return {
        "from": start.isoformat(), "to": end.isoformat(),
        "items": [{"employee_id": i, "name": n, "department": d, "late_count": int(lates_n), "days_checked_in": int(c)}
                  for i, n, d, lates_n, c in rows],
    }


def today_snapshot(ctx: Ctx, department_id: int | None = None) -> dict[str, Any]:
    from .attendance import daily_board

    board = daily_board(ctx, ctx.today, department_id)
    c = board["counts"]
    late_today = sum(1 for r in board["rows"] if r["status"] == S.LATE)
    present_today = sum(c.get(s, 0) for s in (S.PRESENT, S.LATE, S.HALF_DAY, S.MISSING_CHECKOUT))
    pending_leave = ctx.db.scalar(select(func.count(LeaveRequest.id)).where(LeaveRequest.status == RequestStatus.PENDING)) or 0
    pending_corr = ctx.db.scalar(
        select(func.count(AttendanceCorrection.id)).where(AttendanceCorrection.status == RequestStatus.PENDING)) or 0
    return {
        "date": board["date"], "is_working_day": board["is_working_day"],
        "headcount": len(board["rows"]), "present": present_today, "late": late_today,
        "on_leave": c.get(S.ON_LEAVE, 0), "not_checked_in": c.get(S.NOT_CHECKED_IN, 0),
        "pending_approvals": int(pending_leave) + int(pending_corr),
        "pending_leave": int(pending_leave), "pending_corrections": int(pending_corr),
    }


# ------------------------------------------------------------------ leave analytics
def leave_by_type(ctx: Ctx, start: date, end: date, department_id: int | None = None) -> list[dict[str, Any]]:
    employees = _employees_in_scope(ctx, start, end, department_id, None)
    by_emp = approved_leaves_between(ctx, start, end, employee_ids=[e.id for e in employees])
    totals = Counter()
    for days in by_emp.values():
        for code in days.values():
            totals[code] += 1
    types = ctx.db.scalars(select(LeaveType).order_by(LeaveType.id)).all()
    return [{"leave_type": t.code, "name": t.name, "approved_days": totals.get(t.code, 0)} for t in types]


def leave_utilization(ctx: Ctx, year: int, department_id: int | None = None) -> list[dict[str, Any]]:
    """Approved days / (quota x headcount) per leave type and department, for a calendar year."""
    start, end = date(year, 1, 1), date(year, 12, 31)
    stmt = (
        select(Department.id, Department.name, LeaveType.code, LeaveType.yearly_quota,
               func.coalesce(func.sum(LeaveRequest.days), 0))
        .select_from(LeaveRequest)
        .join(Employee, Employee.id == LeaveRequest.employee_id)
        .join(Department, Department.id == Employee.department_id)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
        .where(LeaveRequest.status == RequestStatus.APPROVED, LeaveRequest.start_date >= start,
               LeaveRequest.start_date <= end)
        .group_by(Department.id, Department.name, LeaveType.code, LeaveType.yearly_quota)
    )
    if department_id:
        stmt = stmt.where(Employee.department_id == department_id)
    ref_day = min(end, ctx.today)
    out = []
    for dept_id, dept_name, code, quota, days in ctx.db.execute(stmt):
        heads = headcount_on(ctx, ref_day, dept_id)
        capacity = quota * heads if quota is not None else None
        out.append({"department": dept_name, "leave_type": code, "approved_days": int(days),
                    "quota_per_employee": quota, "headcount": heads,
                    "utilization_pct": _pct(int(days), capacity) if capacity else None})
    return sorted(out, key=lambda r: (r["department"], r["leave_type"]))


# ------------------------------------------------------------------ payroll analytics
def payroll_cost(ctx: Ctx, start: date, end: date, department_id: int | None = None, *,
                 finalized_only: bool = False) -> dict[str, Any]:
    require_roles(ctx, Role.ADMIN, Role.HR)
    stmt = (
        select(PayrollRun.month, PayrollRun.status, Department.name,
               func.sum(Payslip.prorated_gross), func.sum(Payslip.net), func.count(Payslip.id))
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .join(Department, Department.id == Payslip.department_id)
        .where(PayrollRun.month >= month_key(start), PayrollRun.month <= month_key(end))
        .group_by(PayrollRun.month, PayrollRun.status, Department.name)
        .order_by(PayrollRun.month)
    )
    if department_id:
        stmt = stmt.where(Payslip.department_id == department_id)
    if finalized_only:
        stmt = stmt.where(PayrollRun.status == PayrollStatus.FINALIZED)
    rows = [{"month": m, "status": st, "department": d, "gross": float(g or 0), "net": float(n or 0), "employees": int(c)}
            for m, st, d, g, n, c in ctx.db.execute(stmt)]
    by_dept: dict[str, dict[str, float]] = defaultdict(lambda: {"gross": 0.0, "net": 0.0})
    by_month: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_dept[r["department"]]["gross"] += r["gross"]
        by_dept[r["department"]]["net"] += r["net"]
        m = by_month.setdefault(r["month"], {"month": r["month"], "status": r["status"], "gross": 0.0, "net": 0.0})
        m["gross"] += r["gross"]
        m["net"] += r["net"]
    return {
        "rows": rows,
        "by_department": [{"department": k, "gross": round(v["gross"], 2), "net": round(v["net"], 2)}
                          for k, v in sorted(by_dept.items())],
        "by_month": [{**v, "gross": round(v["gross"], 2), "net": round(v["net"], 2)} for v in by_month.values()],
        "total_gross": round(sum(r["gross"] for r in rows), 2),
        "total_net": round(sum(r["net"] for r in rows), 2),
    }


def average_salary(ctx: Ctx, d: date, department_id: int | None = None) -> dict[str, Any]:
    require_roles(ctx, Role.ADMIN, Role.HR)
    latest = (
        select(SalaryStructure.employee_id, func.max(SalaryStructure.id).label("sid"))
        .where(SalaryStructure.effective_from <= d)
        .group_by(SalaryStructure.employee_id)
        .subquery()
    )
    base = (
        select(Department.name, func.coalesce(Designation.name, "Unassigned"), func.avg(SalaryStructure.gross, type_=Money()),
               func.count(Employee.id))
        .select_from(Employee)
        .join(latest, latest.c.employee_id == Employee.id)
        .join(SalaryStructure, SalaryStructure.id == latest.c.sid)
        .join(Department, Department.id == Employee.department_id)
        .outerjoin(Designation, Designation.id == Employee.designation_id)
        .where(_employed_on_clause(d))
    )
    if department_id:
        base = base.where(Employee.department_id == department_id)
    rows = ctx.db.execute(base.group_by(Department.name, Designation.name)).all()
    overall_stmt = (
        select(func.avg(SalaryStructure.gross, type_=Money()), func.count(Employee.id))
        .select_from(Employee)
        .join(latest, latest.c.employee_id == Employee.id)
        .join(SalaryStructure, SalaryStructure.id == latest.c.sid)
        .where(_employed_on_clause(d))
    )
    if department_id:
        overall_stmt = overall_stmt.where(Employee.department_id == department_id)
    overall, n = ctx.db.execute(overall_stmt).one()
    return {
        "average_gross": round(float(overall or 0), 2), "employees": int(n or 0),
        "by_department_designation": [
            {"department": dep, "designation": des, "average_gross": round(float(avg or 0), 2), "employees": int(c)}
            for dep, des, avg, c in rows
        ],
    }


# ------------------------------------------------------------------ dashboards
def _range(ctx: Ctx, date_from: date | None, date_to: date | None) -> tuple[date, date]:
    return resolve_range(ctx.today, date_from, date_to, "this_month")


def hr_dashboard(ctx: Ctx, *, date_from: date | None = None, date_to: date | None = None,
                 department_id: int | None = None) -> dict[str, Any]:
    from . import comms

    require_roles(ctx, Role.ADMIN, Role.HR)
    start, end = _range(ctx, date_from, date_to)
    snapshot = today_snapshot(ctx, department_id)
    stats = attendance_stats(ctx, date_from=start, date_to=end, department_id=department_id)
    return {
        "filters": {"from": start.isoformat(), "to": end.isoformat(), "department_id": department_id},
        "kpis": {
            "headcount": headcount_on(ctx, min(end, ctx.today), department_id),
            "present_today": snapshot["present"], "on_leave_today": snapshot["on_leave"],
            "late_today": snapshot["late"], "not_checked_in_today": snapshot["not_checked_in"],
            "pending_approvals": snapshot["pending_approvals"],
            "attendance_rate_pct": stats["attendance_rate_pct"], "late_pct": stats["late_pct"],
            "absenteeism_pct": stats["absenteeism_pct"],
        },
        "attendance_period": stats,
        "attendance_trend": attendance_trend(ctx, start, end, department_id),
        "absenteeism_by_department": attendance_by_department(ctx, start, end, department_id),
        "leave_by_type": leave_by_type(ctx, start, end, department_id),
        "top_late_comers": top_late_comers(ctx, date_from=start, date_to=end, department_id=department_id)["items"],
        "announcements": comms.list_announcements(ctx, limit=5),
    }


def admin_dashboard(ctx: Ctx, *, date_from: date | None = None, date_to: date | None = None,
                    department_id: int | None = None) -> dict[str, Any]:
    from . import audit, comms

    require_roles(ctx, Role.ADMIN)
    start, end = _range(ctx, date_from, date_to)
    ref = min(end, ctx.today)
    ytd = attrition(ctx, date(ctx.today.year, 1, 1), ctx.today, department_id)
    cost = payroll_cost(ctx, start, end, department_id)
    avg = average_salary(ctx, ref, department_id)
    stats = attendance_stats(ctx, date_from=start, date_to=end, department_id=department_id)
    return {
        "filters": {"from": start.isoformat(), "to": end.isoformat(), "department_id": department_id},
        "kpis": {
            "headcount": headcount_on(ctx, ref, department_id),
            "attrition_ytd_pct": ytd["attrition_pct"], "exits_ytd": ytd["exits"],
            "payroll_cost_gross": cost["total_gross"], "payroll_cost_net": cost["total_net"],
            "average_salary_gross": avg["average_gross"],
            "attendance_rate_pct": stats["attendance_rate_pct"],
        },
        "headcount_by_department": headcount_by(ctx, ref, "department", department_id),
        "payroll_cost_by_department": cost["by_department"],
        "payroll_by_month": cost["by_month"],
        "average_salary": avg["by_department_designation"],
        "gender": headcount_by(ctx, ref, "gender", department_id),
        "tenure": tenure_buckets(ctx, ref, department_id),
        "attrition_ytd": ytd,
        "audit_feed": audit.list_entries(ctx, size=8)["items"],
        "announcements": comms.list_announcements(ctx, limit=5),
    }


def employee_dashboard(ctx: Ctx) -> dict[str, Any]:
    from . import attendance, comms, leave, payroll

    principal = ctx.actor
    if principal.employee_id is None:
        raise ValidationFailed("No employee profile for this account")
    today = ctx.today
    week_start = today - timedelta(days=today.weekday())
    minutes = ctx.db.scalar(
        select(func.coalesce(func.sum(AttendanceRecord.worked_minutes), 0)).where(
            AttendanceRecord.employee_id == principal.employee_id,
            AttendanceRecord.work_date >= week_start, AttendanceRecord.work_date <= today)
    ) or 0
    month = attendance_stats(ctx, period="this_month")
    slips = payroll.list_payslips(ctx, limit=1)
    return {
        "today": attendance.today_status(ctx),
        "hours_this_week": round(int(minutes) / 60, 1), "weekly_target_hours": 40,
        "late_count_this_month": month["late_arrivals"],
        "attendance_this_month": month,
        "leave_balances": leave.balances(ctx),
        "last_payslip": slips[0] if slips else None,
        "announcements": comms.list_announcements(ctx, limit=5),
    }


def payroll_summary(ctx: Ctx, month: str | None = None, department_id: int | None = None,
                    employee_id: int | None = None) -> dict[str, Any]:
    """Used by the AI tool. Employees get only their own latest finalized payslip."""
    from . import payroll

    principal = ctx.actor
    if not principal.sees_all_employees:
        slips = payroll.list_payslips(ctx, month=month, limit=1)
        return {"scope": "own", "payslip": slips[0] if slips else None}
    run = None
    if month:
        run = ctx.db.scalar(select(PayrollRun).where(PayrollRun.month == month))
    else:
        run = ctx.db.scalar(select(PayrollRun).order_by(PayrollRun.month.desc()).limit(1))
    if run is None:
        return {"scope": "company", "run": None, "message": "No payroll run found"}
    run_obj = payroll._get_run(ctx, run.id)
    data = payroll.serialize_run(run_obj)
    slips = data.pop("payslips")
    dept_of = {p.employee_id: p.department_id for p in run_obj.payslips}
    names = dict(ctx.db.execute(select(Department.id, Department.name)).all())
    if department_id:
        slips = [s for s in slips if dept_of.get(s["employee_id"]) == department_id]
        data["department"] = names.get(department_id)
        data["totals"] = {"employees": len(slips), **{k: round(sum(s[src] for s in slips), 2) for k, src in
                          (("gross", "prorated_gross"), ("deductions", "deductions"), ("tax", "tax"), ("net", "net"))}}
    by_dept: dict[str, dict[str, float]] = defaultdict(lambda: {"employees": 0, "gross": 0.0, "net": 0.0})
    for s in slips:
        d = by_dept[names.get(dept_of.get(s["employee_id"]), "?")]
        d["employees"] += 1
        d["gross"] += s["prorated_gross"]
        d["net"] += s["net"]
    result: dict[str, Any] = {"scope": "company", "run": data,
                              "by_department": {k: {**v, "gross": round(v["gross"], 2), "net": round(v["net"], 2)}
                                                for k, v in sorted(by_dept.items())}}
    if employee_id:
        result["payslips"] = [s for s in slips if s["employee_id"] == employee_id]
    return result


def recent_audit(ctx: Ctx, limit: int = 10) -> list[dict[str, Any]]:
    require_roles(ctx, Role.ADMIN)
    rows = ctx.db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
    from .audit import serialize

    return [serialize(r) for r in rows]
