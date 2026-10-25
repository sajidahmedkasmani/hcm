"""M4 — Attendance: web check-in/out (server-stamped), status rules, calendar, daily board,
corrections, and the optional camera + geofence check-in.

Rules (Section 3): one record per employee per day (second check-in = 409); late after
office start + grace (09:15); half_day under 5 h; absent if no record and no approved leave;
missing_checkout if no check-out (derived for past days).
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from ..context import Ctx, assert_can_view_employee, require_roles, require_self_employee
from ..errors import Conflict, Forbidden, NotFound, ValidationFailed
from ..models import (
    AttendanceCorrection,
    AttendanceRecord,
    Employee,
    EmploymentStatus,
    RequestStatus,
    Role,
)
from ..models import (
    AttendanceStatus as S,
)
from . import audit, comms
from .common import get_company, load_calendar, month_bounds, parse_hhmm, snapshot
from .leave import approved_leave_on, approved_leaves_between

MAX_GPS_ACCURACY_M = 100.0


# ------------------------------------------------------------------ pure rules (unit tested)
def rules_from_company(company: Any) -> dict[str, Any]:
    return {
        "office_start": company.office_start,
        "grace_minutes": company.grace_minutes,
        "half_day_hours": company.half_day_hours,
    }


def compute_status(check_in: datetime, check_out: datetime | None, rules: dict[str, Any]) -> tuple[str, bool, int | None]:
    """Returns (status, is_late, worked_minutes). Minute granularity: 09:15 is on time, 09:16 is late."""
    office_start = datetime.combine(check_in.date(), parse_hhmm(rules["office_start"]))
    late_after = office_start + timedelta(minutes=int(rules["grace_minutes"]))
    is_late = check_in.replace(second=0, microsecond=0) > late_after
    status = S.LATE if is_late else S.PRESENT
    worked: int | None = None
    if check_out is not None:
        worked = max(0, int((check_out - check_in).total_seconds() // 60))
        if worked < float(rules["half_day_hours"]) * 60:
            status = S.HALF_DAY
    return status, is_late, worked


def effective_status(record: AttendanceRecord, today: date) -> str:
    if record.check_out is None and record.work_date < today and record.status in (S.PRESENT, S.LATE):
        return S.MISSING_CHECKOUT
    return record.status


def effective_status_expr(today: date):
    """Same rule as `effective_status`, as a SQL CASE expression for database aggregation."""
    return case(
        (
            and_(
                AttendanceRecord.check_out.is_(None),
                AttendanceRecord.work_date < today,
                AttendanceRecord.status.in_([S.PRESENT, S.LATE]),
            ),
            literal(S.MISSING_CHECKOUT.value),
        ),
        else_=AttendanceRecord.status,
    )


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ------------------------------------------------------------------ serialization
def _hhmm(dt: datetime | None) -> str | None:
    return dt.strftime("%H:%M") if dt else None


def serialize(record: AttendanceRecord, today: date) -> dict[str, Any]:
    return {
        "id": record.id,
        "employee_id": record.employee_id,
        "date": record.work_date.isoformat(),
        "check_in": _hhmm(record.check_in),
        "check_out": _hhmm(record.check_out),
        "status": effective_status(record, today),
        "is_late": record.is_late,
        "worked_hours": round(record.worked_minutes / 60, 2) if record.worked_minutes is not None else None,
        "source": record.source,
    }


def _record_for(ctx: Ctx, employee_id: int, day: date) -> AttendanceRecord | None:
    return ctx.db.scalar(
        select(AttendanceRecord).where(AttendanceRecord.employee_id == employee_id, AttendanceRecord.work_date == day)
    )


# ------------------------------------------------------------------ check-in / check-out
def _assert_can_check_in(ctx: Ctx, employee_id: int) -> None:
    employee = ctx.db.get(Employee, employee_id)
    if employee is None or employee.status == EmploymentStatus.EXITED:
        raise Forbidden("Inactive employees cannot check in")
    today = ctx.today
    if today < employee.join_date:
        raise Conflict("You cannot check in before your joining date")
    cal = load_calendar(ctx.db)
    if not cal.is_working_day(today):
        label = cal.holidays.get(today) or "a weekend"
        raise Conflict(f"Today is {label} — attendance is only recorded on working days", code="not_working_day")
    existing = _record_for(ctx, employee_id, today)
    if existing is not None:
        raise Conflict(f"Already checked in today at {_hhmm(existing.check_in)}", code="already_checked_in")
    if approved_leave_on(ctx, employee_id, today) is not None:
        raise Conflict("You are on approved leave today; cancel it before checking in", code="on_leave")


def _open_record(ctx: Ctx, employee_id: int) -> AttendanceRecord:
    record = _record_for(ctx, employee_id, ctx.today)
    if record is None or record.check_in is None:
        raise Conflict("You have not checked in today", code="not_checked_in")
    if record.check_out is not None:
        raise Conflict(f"Already checked out today at {_hhmm(record.check_out)}", code="already_checked_out")
    return record


def _record_check_in(ctx: Ctx, employee_id: int, *, source: str, geo: dict[str, Any] | None) -> dict[str, Any]:
    _assert_can_check_in(ctx, employee_id)
    now, today = ctx.now, ctx.today
    rules = rules_from_company(get_company(ctx.db))
    status, is_late, _ = compute_status(now, None, rules)
    record = AttendanceRecord(
        employee_id=employee_id, work_date=today, check_in=now, status=status, is_late=is_late,
        source=source, rules_snapshot=rules, geo=geo,
    )
    ctx.db.add(record)
    try:
        ctx.db.flush()
    except IntegrityError as exc:  # concurrent double submit
        ctx.db.rollback()
        raise Conflict("Already checked in today", code="already_checked_in") from exc
    return serialize(record, today)


def _record_check_out(ctx: Ctx, employee_id: int, *, proof: dict[str, Any] | None) -> dict[str, Any]:
    record = _open_record(ctx, employee_id)
    status, is_late, worked = compute_status(record.check_in, ctx.now, record.rules_snapshot)
    record.check_out, record.status, record.is_late, record.worked_minutes = ctx.now, status, is_late, worked
    if proof is not None:
        record.geo = {**(record.geo or {}), "out": proof}
    return serialize(record, ctx.today)


def _require_web_mode(ctx: Ctx) -> None:
    if get_company(ctx.db).attendance_mode == "camera":
        raise Forbidden("Attendance is marked with the camera (face + office location). Use 'Check in with camera'.",
                        code="camera_required")


def check_in(ctx: Ctx) -> dict[str, Any]:
    """Web button — only when Admin has set the attendance method to 'web' (no camera)."""
    employee_id = require_self_employee(ctx)
    _require_web_mode(ctx)
    return _record_check_in(ctx, employee_id, source="web", geo=None)


def check_out(ctx: Ctx) -> dict[str, Any]:
    employee_id = require_self_employee(ctx)
    _require_web_mode(ctx)
    return _record_check_out(ctx, employee_id, proof=None)


def _geofence(company: Any, lat: float, lng: float, accuracy_m: float) -> dict[str, Any]:
    """The server computes the distance (Haversine) and decides; the phone only sends coordinates."""
    if company.office_lat is None or company.office_lng is None:
        raise ValidationFailed("Admin hasn't set the office location yet", code="no_office_location")
    if accuracy_m > MAX_GPS_ACCURACY_M:
        raise ValidationFailed(f"Location accuracy is {accuracy_m:.0f} m; it must be {MAX_GPS_ACCURACY_M:.0f} m or "
                               "better — turn on precise location and try again", code="low_gps_accuracy")
    distance = haversine_m(company.office_lat, company.office_lng, lat, lng)
    if distance > company.office_radius_m:
        raise ValidationFailed(f"You are {distance:.0f} m from the office (allowed {company.office_radius_m} m)",
                               code="outside_geofence", details={"distance_m": round(distance, 1)})
    return {"lat": round(lat, 6), "lng": round(lng, 6), "accuracy_m": round(accuracy_m, 1), "distance_m": round(distance, 1)}


def camera_attendance(
    ctx: Ctx, *, action: str, frames: list[str], lat: float, lng: float, accuracy_m: float
) -> dict[str, Any]:
    """Check in or out with the camera. Every check runs on the server, cheapest first:
    attendance rules -> office geofence -> exactly one face -> matches the enrolled face -> (optional) blink.
    Frames are discarded after verification; the record keeps only the outcome."""
    from . import face
    from .face_profiles import enrolled_descriptor

    employee_id = require_self_employee(ctx)
    company = get_company(ctx.db)
    if action == "in":
        _assert_can_check_in(ctx, employee_id)
    elif action == "out":
        _open_record(ctx, employee_id)
    else:
        raise ValidationFailed("action must be 'in' or 'out'")
    geo = _geofence(company, lat, lng, accuracy_m)
    enrolled = enrolled_descriptor(ctx, employee_id)
    if enrolled is None:
        raise Conflict("Set up face check-in first (Home → Set up face check-in), or ask HR to enroll you",
                       code="face_not_enrolled")
    seen = face.inspect_frames(frames)
    match = statistics.median(face.distance(d, enrolled) for d in seen.best_descriptors)
    if match > ctx.settings.face_match_threshold:
        audit.record(ctx, "reject", "camera_attendance", employee_id,
                     after={"reason": "face_mismatch", "action": action, "distance": round(match, 3)})
        ctx.db.commit()  # keep evidence of the attempt although the request fails
        raise ValidationFailed("Your face doesn't match the face enrolled for this account. Try again facing the "
                               "camera in good light, or ask HR to re-enroll you.", code="face_mismatch",
                               details={"distance": round(match, 3)})
    liveness = None
    if company.require_blink:
        liveness = face.blink_detected(seen.ears)
        if not liveness:
            raise ValidationFailed("We couldn't see a blink — look at the camera and blink once while it captures",
                                   code="no_blink")
    proof = {**geo, "face_distance": round(match, 3), "frames": seen.frames, "frames_with_face": seen.frames_with_face,
             "liveness": liveness, "at": ctx.now.isoformat()}
    if action == "in":
        return _record_check_in(ctx, employee_id, source="camera", geo={"in": proof})
    return _record_check_out(ctx, employee_id, proof=proof)


def today_status(ctx: Ctx) -> dict[str, Any]:
    employee_id = require_self_employee(ctx)
    today = ctx.today
    cal = load_calendar(ctx.db)
    record = _record_for(ctx, employee_id, today)
    leave = approved_leave_on(ctx, employee_id, today)
    company = get_company(ctx.db)
    from .face_profiles import enrolled_descriptor

    return {
        "attendance_mode": company.attendance_mode,
        "require_blink": company.require_blink,
        "office_location_set": company.office_lat is not None and company.office_lng is not None,
        "face_enrolled": enrolled_descriptor(ctx, employee_id) is not None,
        "date": today.isoformat(),
        "server_time": ctx.now.strftime("%H:%M"),
        "is_working_day": cal.is_working_day(today),
        "holiday": cal.holidays.get(today),
        "on_leave": leave.leave_type.code if leave else None,
        "record": serialize(record, today) if record else None,
        "can_check_in": record is None and leave is None,
        "can_check_out": record is not None and record.check_out is None,
    }


# ------------------------------------------------------------------ calendar & board
def _employed_on(e: Employee, d: date) -> bool:
    return e.join_date <= d and (e.exit_date is None or d <= e.exit_date)


def day_status(
    ctx: Ctx, employee: Employee, d: date, record: AttendanceRecord | None, leave_code: str | None, cal: Any
) -> str:
    today = ctx.today
    if record is not None:
        return effective_status(record, today)
    if leave_code:
        return S.ON_LEAVE
    if cal.is_holiday(d):
        return S.HOLIDAY
    if cal.is_weekend(d):
        return S.WEEKEND
    if d > today:
        return S.UPCOMING
    if d == today:
        return S.NOT_CHECKED_IN
    if d < cal.tracking_start(employee.join_date, employee.created_at):
        return "not_tracked"
    return S.ABSENT


def calendar(ctx: Ctx, month: str, employee_id: int | None = None) -> dict[str, Any]:
    principal = ctx.actor
    employee_id = employee_id or principal.employee_id
    if employee_id is None:
        raise ValidationFailed("employee_id is required")
    assert_can_view_employee(ctx, employee_id)
    employee = ctx.db.get(Employee, employee_id)
    if employee is None:
        raise NotFound("Employee not found")
    start, end = month_bounds(month)
    cal = load_calendar(ctx.db)
    records = {
        r.work_date: r
        for r in ctx.db.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.employee_id == employee_id,
                AttendanceRecord.work_date >= start,
                AttendanceRecord.work_date <= end,
            )
        )
    }
    leave_days = approved_leaves_between(ctx, start, end, employee_ids=[employee_id]).get(employee_id, {})
    days = []
    d = start
    while d <= end:
        rec = records.get(d)
        status = day_status(ctx, employee, d, rec, leave_days.get(d), cal) if _employed_on(employee, d) else "not_employed"
        days.append({
            "date": d.isoformat(),
            "weekday": d.strftime("%a"),
            "status": status,
            "leave_type": leave_days.get(d),
            "holiday": cal.holidays.get(d),
            "check_in": _hhmm(rec.check_in) if rec else None,
            "check_out": _hhmm(rec.check_out) if rec else None,
            "worked_hours": round(rec.worked_minutes / 60, 2) if rec and rec.worked_minutes is not None else None,
        })
        d += timedelta(days=1)
    summary: dict[str, int] = {}
    for item in days:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    return {"employee_id": employee_id, "month": month, "days": days, "summary": summary}


def daily_board(ctx: Ctx, day: date | None = None, department_id: int | None = None) -> dict[str, Any]:
    require_roles(ctx, Role.ADMIN, Role.HR)
    day = day or ctx.today
    cal = load_calendar(ctx.db)
    stmt = select(Employee).options(joinedload(Employee.department)).order_by(Employee.full_name)
    if department_id:
        stmt = stmt.where(Employee.department_id == department_id)
    employees = [e for e in ctx.db.scalars(stmt) if _employed_on(e, day)]
    ids = [e.id for e in employees]
    records = {
        r.employee_id: r
        for r in ctx.db.scalars(
            select(AttendanceRecord).where(AttendanceRecord.work_date == day, AttendanceRecord.employee_id.in_(ids))
        )
    } if ids else {}
    leaves = approved_leaves_between(ctx, day, day, employee_ids=ids) if ids else {}
    rows, counts = [], {}
    for e in employees:
        rec = records.get(e.id)
        leave_code = leaves.get(e.id, {}).get(day)
        status = day_status(ctx, e, day, rec, leave_code, cal)
        counts[status] = counts.get(status, 0) + 1
        rows.append({
            "employee_id": e.id,
            "employee_code": e.employee_code,
            "full_name": e.full_name,
            "department": e.department.name,
            "status": status,
            "leave_type": leave_code,
            "check_in": _hhmm(rec.check_in) if rec else None,
            "check_out": _hhmm(rec.check_out) if rec else None,
            "source": rec.source if rec else None,
        })
    return {"date": day.isoformat(), "is_working_day": cal.is_working_day(day), "counts": counts, "rows": rows}


# ------------------------------------------------------------------ corrections
def serialize_correction(c: AttendanceCorrection, employee_name: str | None = None) -> dict[str, Any]:
    return {
        "id": c.id,
        "employee_id": c.employee_id,
        "employee_name": employee_name,
        "date": c.work_date.isoformat(),
        "check_in": _hhmm(c.requested_check_in),
        "check_out": _hhmm(c.requested_check_out),
        "reason": c.reason,
        "status": c.status,
        "review_comment": c.review_comment,
        "created_at": c.created_at.isoformat(),
    }


def request_correction(
    ctx: Ctx, *, work_date: date, check_in: str, check_out: str | None, reason: str
) -> dict[str, Any]:
    employee_id = require_self_employee(ctx)
    employee = ctx.db.get(Employee, employee_id)
    assert employee is not None
    if work_date > ctx.today:
        raise ValidationFailed("Corrections can only be requested for today or past days")
    if work_date < employee.join_date:
        raise ValidationFailed("Date is before your joining date")
    if not load_calendar(ctx.db).is_working_day(work_date):
        raise ValidationFailed("Corrections are only possible for working days")
    if approved_leave_on(ctx, employee_id, work_date) is not None:
        raise ValidationFailed("You were on approved leave that day")
    if len(reason.strip()) < 5:
        raise ValidationFailed("Please give a reason (at least 5 characters)")
    cin = datetime.combine(work_date, parse_hhmm(check_in))
    cout = datetime.combine(work_date, parse_hhmm(check_out)) if check_out else None
    if cout is not None and cout <= cin:
        raise ValidationFailed("Check-out must be after check-in")
    pending = ctx.db.scalar(
        select(AttendanceCorrection.id).where(
            AttendanceCorrection.employee_id == employee_id,
            AttendanceCorrection.work_date == work_date,
            AttendanceCorrection.status == RequestStatus.PENDING,
        )
    )
    if pending:
        raise Conflict("A correction for this date is already pending")
    c = AttendanceCorrection(
        employee_id=employee_id, work_date=work_date, requested_check_in=cin, requested_check_out=cout,
        reason=reason.strip(), status=RequestStatus.PENDING, created_at=ctx.now,
    )
    ctx.db.add(c)
    ctx.db.flush()
    comms.notify_role(ctx, Role.HR, "correction_request", "Attendance correction request",
                      f"{employee.full_name} requested a correction for {work_date.isoformat()}",
                      exclude_user_id=ctx.actor.user_id)
    audit.record(ctx, "create", "attendance_correction", c.id, after=snapshot(c))
    return serialize_correction(c, employee.full_name)


def list_corrections(ctx: Ctx, status: str | None = None) -> list[dict[str, Any]]:
    principal = ctx.actor
    stmt = select(AttendanceCorrection, Employee.full_name).join(Employee, Employee.id == AttendanceCorrection.employee_id)
    if not principal.sees_all_employees:
        stmt = stmt.where(AttendanceCorrection.employee_id == (principal.employee_id or -1))
    if status:
        stmt = stmt.where(AttendanceCorrection.status == status)
    rows = ctx.db.execute(stmt.order_by(AttendanceCorrection.id.desc())).all()
    return [serialize_correction(c, name) for c, name in rows]


def decide_correction(ctx: Ctx, correction_id: int, *, approve: bool, comment: str | None = None) -> dict[str, Any]:
    principal = require_roles(ctx, Role.HR)
    c = ctx.db.get(AttendanceCorrection, correction_id)
    if c is None:
        raise NotFound("Correction not found")
    if c.status != RequestStatus.PENDING:
        raise Conflict(f"Correction is already {c.status}")
    if c.employee_id == principal.employee_id:
        raise Forbidden("You cannot approve your own correction")
    employee = ctx.db.get(Employee, c.employee_id)
    assert employee is not None
    before = snapshot(c)
    c.status = RequestStatus.APPROVED if approve else RequestStatus.REJECTED
    c.reviewer_user_id, c.review_comment, c.decided_at = principal.user_id, comment, ctx.now
    if approve:
        record = _record_for(ctx, c.employee_id, c.work_date)
        rec_before = snapshot(record) if record else None
        if record is None:
            record = AttendanceRecord(
                employee_id=c.employee_id, work_date=c.work_date,
                rules_snapshot=rules_from_company(get_company(ctx.db)), status=S.PRESENT,
            )
            ctx.db.add(record)
        status, is_late, worked = compute_status(c.requested_check_in, c.requested_check_out, record.rules_snapshot)
        record.check_in, record.check_out = c.requested_check_in, c.requested_check_out
        record.status, record.is_late, record.worked_minutes, record.source = status, is_late, worked, "correction"
        ctx.db.flush()
        audit.record(ctx, "update", "attendance", record.id, before=rec_before, after=snapshot(record))
    audit.record(ctx, "approve" if approve else "reject", "attendance_correction", c.id, before=before, after=snapshot(c))
    comms.notify(ctx, employee.user_id, "correction_decision",
                 f"Attendance correction {c.status}",
                 f"Your correction for {c.work_date.isoformat()} was {c.status}" + (f": {comment}" if comment else ""))
    return serialize_correction(c, employee.full_name)
