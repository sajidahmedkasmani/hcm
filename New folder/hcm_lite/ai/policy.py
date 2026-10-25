"""Policy Q&A grounding: the business rules (Section 3) rendered from the *live* company settings
and placed in the system prompt. The model must answer policy questions only from this text."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from ..context import Ctx
from ..models import Holiday, LeaveType
from ..services.common import get_company, weekend_set

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def build_policy_text(ctx: Ctx) -> str:
    c = get_company(ctx.db)
    start = datetime.strptime(c.office_start, "%H:%M")
    late_after = (start + timedelta(minutes=c.grace_minutes)).strftime("%H:%M")
    weekend = ", ".join(DAY_NAMES[d] for d in sorted(weekend_set(c))) or "none"
    types = ctx.db.scalars(select(LeaveType).order_by(LeaveType.id)).all()
    quotas = ", ".join(
        f"{t.name} {t.yearly_quota} days" if t.yearly_quota is not None else f"{t.name} unlimited" for t in types
    ) or "not configured yet"
    upcoming = ctx.db.scalars(select(Holiday).where(Holiday.day >= ctx.today).order_by(Holiday.day).limit(6)).all()
    holidays = "; ".join(f"{h.name} on {h.day.isoformat()}" for h in upcoming) or "none announced"
    geofence = (f"within {c.office_radius_m} m of the office location set by Admin"
                if c.office_lat is not None else "within the office radius once Admin sets the office location")
    rules = [
        f"Office hours: work starts at {c.office_start} with a {c.grace_minutes}-minute grace period; a check-in after {late_after} is marked late.",
        f"Half day: a day with less than {c.half_day_hours:g} hours between check-in and check-out is a half day.",
        f"Weekend days are {weekend}. Upcoming public holidays: {holidays}.",
        "Attendance: one record per employee per day and the server stamps the time; a second check-in on the same day is rejected.",
        "Absent means a working day with no attendance record and no approved leave; missing checkout means checked in but never checked out.",
        "Attendance correction: an employee requests a correction with a reason and HR approves or rejects it.",
        f"Camera attendance needs exactly one face, GPS accuracy of 100 m or better, and a location {geofence}.",
        "Leave counts working days only: weekends and public holidays inside the range are skipped.",
        f"Yearly leave quotas: {quotas}. Leave that overlaps another request or exceeds the balance is rejected.",
        "Leave balance = yearly quota minus approved days; pending requests are reserved until decided.",
        "HR approves or rejects leave with a comment. Employees can cancel pending leave, or approved leave that has not started.",
        "Salary structure: gross salary = 1.5 x basic, made of basic + house rent allowance (40% of basic) + medical allowance (10% of basic).",
        "Payroll deductions: per-day rate = gross / working days in the month; deduct absent days + unpaid leave days + 0.5 x half days.",
        "Joiners and leavers are prorated by the working days they were employed in the month.",
        "Income tax (illustrative) on annual gross = monthly gross x 12: up to 600,000 no tax; 600,000 to 1,200,000 5% of the amount above 600,000; "
        "1,200,000 to 2,400,000 30,000 + 15% above 1,200,000; 2,400,000 to 3,600,000 210,000 + 25% above 2,400,000; "
        "above 3,600,000 510,000 + 35% above 3,600,000. Monthly tax = annual tax / 12.",
        "HR prepares the monthly payroll draft and only Admin can finalize it; a finalized month is locked and cannot be edited.",
        "Late penalty rule: every 3 late arrivals in a month deduct 0.5 day of pay"
        + (" (currently enabled)." if c.late_penalty_enabled else " (currently disabled)."),
        "Privacy: employees see only their own attendance, leave and payslips; they cannot approve requests or edit their own salary, role or attendance.",
    ]
    return "\n".join(f"- {r}" for r in rules)
