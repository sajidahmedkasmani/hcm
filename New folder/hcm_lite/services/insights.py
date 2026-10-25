"""Bonus insights: attrition risk score and absence anomaly flags.

Both are *explainable heuristics* over the app's own data (no labelled exits exist on a
fresh install, so a supervised model cannot be trained yet). Each score returns the factors
that produced it, so HR can see *why* someone is flagged.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from ..context import Ctx, require_roles
from ..models import AttendanceRecord, Department, Employee, LeaveType, Role
from .analytics import per_employee_attendance
from .common import load_calendar
from .leave import approved_leaves_between

RISK_WEIGHTS = {"absenteeism": 30, "lateness": 20, "unpaid_leave": 15, "short_tenure": 15,
                "half_days": 10, "worsening": 10}


def _absent_dates(ctx: Ctx, start: date, end: date) -> dict[int, list[date]]:
    """Working days in range with no record and no approved leave, per employee (past days only)."""
    cal = load_calendar(ctx.db)
    last = min(end, ctx.today - timedelta(days=1))
    employees = ctx.db.scalars(select(Employee).where(Employee.join_date <= last)).all()
    ids = [e.id for e in employees]
    records: dict[int, set[date]] = defaultdict(set)
    for emp_id, d in ctx.db.execute(
        select(AttendanceRecord.employee_id, AttendanceRecord.work_date)
        .where(AttendanceRecord.work_date >= start, AttendanceRecord.work_date <= last)
    ):
        records[emp_id].add(d)
    leaves = approved_leaves_between(ctx, start, last, employee_ids=ids)
    out: dict[int, list[date]] = {}
    for e in employees:
        window_end = min(last, e.exit_date) if e.exit_date else last
        days = cal.working_days(max(start, cal.tracking_start(e.join_date, e.created_at)), window_end)
        out[e.id] = [d for d in days if d not in records[e.id] and d not in leaves.get(e.id, {})]
    return out


def attrition_risk(ctx: Ctx, lookback_days: int = 60) -> list[dict[str, Any]]:
    require_roles(ctx, Role.ADMIN, Role.HR)
    today = ctx.today
    start = today - timedelta(days=lookback_days)
    mid = today - timedelta(days=lookback_days // 2)
    recent = per_employee_attendance(ctx, mid, today)
    older = per_employee_attendance(ctx, start, mid - timedelta(days=1))
    full = per_employee_attendance(ctx, start, today)
    unpaid_codes = set(ctx.db.scalars(select(LeaveType.code).where(LeaveType.is_paid.is_(False))))
    leaves = approved_leaves_between(ctx, start, today)
    dept_names = dict(ctx.db.execute(select(Department.id, Department.name)).all())
    employees = {e.id: e for e in ctx.db.scalars(select(Employee).where(Employee.exit_date.is_(None)))}

    results = []
    for emp_id, e in employees.items():
        a = full.get(emp_id)
        if a is None or a["expected"] == 0:
            continue
        absent_rate = a["absent"] / a["expected"]
        late_rate = a["late_arrivals"] / a["attended"] if a["attended"] else 0.0
        unpaid = sum(1 for code in leaves.get(emp_id, {}).values() if code in unpaid_codes)
        tenure_months = (today.year - e.join_date.year) * 12 + (today.month - e.join_date.month)
        r, o = recent.get(emp_id), older.get(emp_id)
        worsening = 0.0
        if r and o and r["expected"] and o["expected"]:
            worsening = max(0.0, r["absent"] / r["expected"] - o["absent"] / o["expected"])
        features = {
            "absenteeism": min(absent_rate / 0.15, 1.0),
            "lateness": min(late_rate / 0.40, 1.0),
            "unpaid_leave": min(unpaid / 5, 1.0),
            "short_tenure": 1.0 if tenure_months < 6 else 0.5 if tenure_months < 12 else 0.0,
            "half_days": min((a["half_day"] / a["expected"]) / 0.10, 1.0),
            "worsening": min(worsening / 0.10, 1.0),
        }
        contributions = {k: round(RISK_WEIGHTS[k] * v, 1) for k, v in features.items()}
        score = round(sum(contributions.values()), 1)
        explain = {
            "absenteeism": f"absent {a['absent']} of {a['expected']} working days",
            "lateness": f"late on {a['late_arrivals']} of {a['attended']} days",
            "unpaid_leave": f"{unpaid} unpaid leave day(s)",
            "short_tenure": f"tenure {tenure_months} month(s)",
            "half_days": f"{a['half_day']} half day(s)",
            "worsening": "absences rising in the last few weeks",
        }
        factors = [explain[k] for k, v in sorted(contributions.items(), key=lambda kv: -kv[1]) if v > 0][:3]
        results.append({
            "employee_id": emp_id, "name": e.full_name, "department": dept_names.get(e.department_id),
            "score": score, "level": "high" if score >= 60 else "medium" if score >= 35 else "low",
            "factors": factors, "contributions": contributions,
        })
    return sorted(results, key=lambda r: -r["score"])


def absence_anomalies(ctx: Ctx, lookback_days: int = 60) -> list[dict[str, Any]]:
    require_roles(ctx, Role.ADMIN, Role.HR)
    today = ctx.today
    start = today - timedelta(days=lookback_days)
    cal = load_calendar(ctx.db)
    absent = _absent_dates(ctx, start, today)
    names = dict(ctx.db.execute(select(Employee.id, Employee.full_name)).all())
    rates = {}
    for emp_id, days in absent.items():
        e = ctx.db.get(Employee, emp_id)
        expected = cal.count(max(start, cal.tracking_start(e.join_date, e.created_at)),
                             min(today - timedelta(days=1), e.exit_date or today))
        if expected:
            rates[emp_id] = len(days) / expected
    mean = statistics.fmean(rates.values()) if rates else 0.0
    std = statistics.pstdev(rates.values()) if len(rates) >= 3 else 0.0
    flags = []
    for emp_id, days in absent.items():
        reasons = []
        rate = rates.get(emp_id, 0.0)
        if std > 0 and (rate - mean) / std >= 2:
            reasons.append(f"absence rate {rate:.0%} is >2σ above the company mean ({mean:.0%})")
        elif rate >= 0.20 and len(days) >= 3:
            reasons.append(f"absence rate {rate:.0%} (>= 20%)")
        mon_fri = [d for d in days if d.weekday() in (0, 4)]
        if len(days) >= 3 and len(mon_fri) / len(days) >= 0.6:
            reasons.append(f"{len(mon_fri)} of {len(days)} absences fall on Monday/Friday")
        run = best = 0
        prev: date | None = None
        for d in sorted(days):
            if prev is not None and cal.count(prev + timedelta(days=1), d) == 1:
                run += 1
            else:
                run = 1
            best = max(best, run)
            prev = d
        if best >= 3:
            reasons.append(f"{best} consecutive working days absent")
        if reasons:
            flags.append({"employee_id": emp_id, "name": names.get(emp_id), "absent_days": len(days),
                          "absence_rate_pct": round(rate * 100, 1), "reasons": reasons})
    return sorted(flags, key=lambda f: -f["absent_days"])
