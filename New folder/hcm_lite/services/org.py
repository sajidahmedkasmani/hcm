"""M2 — Organization setup: first-run, company settings, departments, designations,
leave types (yearly quotas) and public holidays. Admin manages; HR views."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..context import Ctx, Principal, require_authenticated, require_roles
from ..errors import Conflict, NotFound, ValidationFailed
from ..models import Company, Department, Designation, Employee, Holiday, LeaveRequest, LeaveType, Role, User
from ..security import hash_password, validate_password_strength
from . import audit
from .common import get_company, parse_hhmm, snapshot

STANDARD_LEAVE_TYPES: tuple[tuple[str, str, int | None, bool], ...] = (
    ("annual", "Annual leave", 14, True),
    ("sick", "Sick leave", 8, True),
    ("casual", "Casual leave", 10, True),
    ("unpaid", "Unpaid leave", None, False),
)


# ------------------------------------------------------------------ first run
def is_initialized(ctx: Ctx) -> bool:
    has_company = ctx.db.scalar(select(func.count(Company.id))) or 0
    has_admin = ctx.db.scalar(select(func.count(User.id)).where(User.role == Role.ADMIN)) or 0
    return bool(has_company and has_admin)


def first_run(
    ctx: Ctx,
    *,
    company_name: str,
    admin_email: str,
    admin_password: str,
    admin_name: str = "Administrator",
    create_standard_leave_types: bool = True,
) -> dict[str, Any]:
    """Creates the company profile and the Admin account. Only allowed on a fresh install."""
    if is_initialized(ctx):
        raise Conflict("Setup has already been completed", code="already_initialized")
    if not company_name.strip():
        raise ValidationFailed("Company name is required")
    validate_password_strength(admin_password)

    company = Company(name=company_name.strip(), created_at=ctx.now)
    admin = User(
        email=admin_email.strip().lower(),
        full_name=admin_name.strip() or "Administrator",
        password_hash=hash_password(admin_password, ctx.settings.bcrypt_rounds),
        role=Role.ADMIN,
        is_active=True,
        must_change_password=False,
        created_at=ctx.now,
    )
    ctx.db.add_all([company, admin])
    if create_standard_leave_types:
        for code, name, quota, paid in STANDARD_LEAVE_TYPES:
            ctx.db.add(LeaveType(code=code, name=name, yearly_quota=quota, is_paid=paid))
    ctx.db.flush()
    actor = ctx.with_principal(Principal(user_id=admin.id, role=Role.ADMIN, email=admin.email, full_name=admin.full_name))
    audit.record(actor, "create", "company", company.id, after=snapshot(company))
    audit.record(actor, "create", "user", admin.id, after={"email": admin.email, "role": admin.role})
    return {"company": serialize_company(company), "admin": {"id": admin.id, "email": admin.email}}


def bootstrap_from_env(ctx: Ctx) -> bool:
    """First run from ADMIN_EMAIL / ADMIN_PASSWORD (and optional COMPANY_NAME) env variables."""
    s = ctx.settings
    if is_initialized(ctx) or not (s.admin_email and s.admin_password):
        return False
    first_run(
        ctx,
        company_name=s.company_name or "My Company",
        admin_email=s.admin_email,
        admin_password=s.admin_password,
    )
    return True


# ------------------------------------------------------------------ company settings
def serialize_company(c: Company) -> dict[str, Any]:
    return {
        "name": c.name,
        "office_start": c.office_start,
        "grace_minutes": c.grace_minutes,
        "half_day_hours": c.half_day_hours,
        "weekend_days": [int(x) for x in c.weekend_days.split(",") if x != ""],
        "office_lat": c.office_lat,
        "office_lng": c.office_lng,
        "office_radius_m": c.office_radius_m,
        "late_penalty_enabled": c.late_penalty_enabled,
    }


def get_settings(ctx: Ctx) -> dict[str, Any]:
    require_roles(ctx, Role.ADMIN, Role.HR)
    return serialize_company(get_company(ctx.db))


def public_profile(ctx: Ctx) -> dict[str, Any]:
    """Minimal company info any signed-in user may see (name, office hours, office location)."""
    c = get_company(ctx.db)
    data = serialize_company(c)
    return {k: data[k] for k in ("name", "office_start", "grace_minutes", "half_day_hours", "weekend_days",
                                 "office_lat", "office_lng", "office_radius_m")}


def update_settings(ctx: Ctx, changes: dict[str, Any]) -> dict[str, Any]:
    """Admin only. Stored as settings and applied to *new* records only (records keep a rules snapshot)."""
    require_roles(ctx, Role.ADMIN)
    company = get_company(ctx.db)
    before = snapshot(company)
    for key, value in changes.items():
        if value is None and key not in {"office_lat", "office_lng"}:
            continue
        if key == "name":
            if not str(value).strip():
                raise ValidationFailed("Company name cannot be empty")
            company.name = str(value).strip()
        elif key == "office_start":
            parse_hhmm(value)
            company.office_start = value
        elif key == "grace_minutes":
            if not 0 <= int(value) <= 120:
                raise ValidationFailed("grace_minutes must be between 0 and 120")
            company.grace_minutes = int(value)
        elif key == "half_day_hours":
            if not 0 < float(value) <= 12:
                raise ValidationFailed("half_day_hours must be between 0 and 12")
            company.half_day_hours = float(value)
        elif key == "weekend_days":
            days = sorted({int(d) for d in value})
            if any(d < 0 or d > 6 for d in days) or len(days) > 3:
                raise ValidationFailed("weekend_days must be up to 3 values between 0 (Mon) and 6 (Sun)")
            company.weekend_days = ",".join(str(d) for d in days)
        elif key in {"office_lat", "office_lng"}:
            if value is not None:
                limit = 90 if key == "office_lat" else 180
                if not -limit <= float(value) <= limit:
                    raise ValidationFailed(f"{key} out of range")
            setattr(company, key, None if value is None else float(value))
        elif key == "office_radius_m":
            if not 10 <= int(value) <= 5000:
                raise ValidationFailed("office_radius_m must be between 10 and 5000 metres")
            company.office_radius_m = int(value)
        elif key == "late_penalty_enabled":
            company.late_penalty_enabled = bool(value)
        else:
            raise ValidationFailed(f"Unknown setting '{key}'")
    ctx.db.flush()
    audit.record(ctx, "update", "company_settings", company.id, before=before, after=snapshot(company))
    return serialize_company(company)


# ------------------------------------------------------------------ generic named lookups
def _create_named(ctx: Ctx, model: type, entity: str, name: str) -> dict[str, Any]:
    require_roles(ctx, Role.ADMIN)
    name = name.strip()
    if not name:
        raise ValidationFailed("Name is required")
    if ctx.db.scalar(select(model).where(func.lower(model.name) == name.lower())):
        raise Conflict(f"{entity.title()} '{name}' already exists")
    obj = model(name=name)
    ctx.db.add(obj)
    ctx.db.flush()
    audit.record(ctx, "create", entity, obj.id, after=snapshot(obj))
    return {"id": obj.id, "name": obj.name}


MAX_BULK_NAMES = 500
_HEADER_WORDS = {"name", "names", "department", "departments", "designation", "designations", "title", "titles"}


def _bulk_create_named(ctx: Ctx, model: type, entity: str, names: list[str]) -> dict[str, Any]:
    """Create many lookup values in one call. Existing names (case-insensitive), repeats within the list,
    blank lines and a header row are skipped — never errors — so the same list can be pasted twice safely."""
    require_roles(ctx, Role.ADMIN)
    if len(names) > MAX_BULK_NAMES:
        raise ValidationFailed(f"At most {MAX_BULK_NAMES} names per request")
    existing = {n.lower() for n in ctx.db.scalars(select(model.name))}
    created, skipped, seen = [], [], set()
    for index, raw in enumerate(names):
        name = " ".join(str(raw).split())  # trim and collapse inner whitespace
        if not name:
            continue
        if index == 0 and name.lower() in _HEADER_WORDS:
            continue  # header row from a spreadsheet column
        key = name.lower()
        if len(name) > 120:
            skipped.append({"name": name[:40] + "…", "reason": "longer than 120 characters"})
        elif key in existing:
            skipped.append({"name": name, "reason": "already exists"})
        elif key in seen:
            skipped.append({"name": name, "reason": "repeated in the list"})
        else:
            obj = model(name=name)
            ctx.db.add(obj)
            ctx.db.flush()
            audit.record(ctx, "create", entity, obj.id, after=snapshot(obj))
            created.append({"id": obj.id, "name": obj.name})
        seen.add(key)
    if not created and not skipped:
        raise ValidationFailed("The list is empty — add one name per line")
    return {"created": created, "skipped": skipped}


def _rename(ctx: Ctx, model: type, entity: str, obj_id: int, name: str) -> dict[str, Any]:
    require_roles(ctx, Role.ADMIN)
    obj = ctx.db.get(model, obj_id)
    if obj is None:
        raise NotFound(f"{entity.title()} not found")
    clash = ctx.db.scalar(select(model).where(func.lower(model.name) == name.strip().lower(), model.id != obj_id))
    if clash:
        raise Conflict(f"{entity.title()} '{name}' already exists")
    before = snapshot(obj)
    obj.name = name.strip()
    audit.record(ctx, "update", entity, obj.id, before=before, after=snapshot(obj))
    return {"id": obj.id, "name": obj.name}


def _delete_named(ctx: Ctx, model: type, entity: str, obj_id: int, in_use_column: Any) -> None:
    require_roles(ctx, Role.ADMIN)
    obj = ctx.db.get(model, obj_id)
    if obj is None:
        raise NotFound(f"{entity.title()} not found")
    in_use = ctx.db.scalar(select(func.count()).where(in_use_column == obj_id)) or 0
    if in_use:
        raise Conflict(f"{entity.title()} is used by {in_use} employee(s) and cannot be deleted")
    before = snapshot(obj)
    ctx.db.delete(obj)
    audit.record(ctx, "delete", entity, obj_id, before=before)


def _list_named(ctx: Ctx, model: type) -> list[dict[str, Any]]:
    require_roles(ctx, Role.ADMIN, Role.HR)
    return [{"id": o.id, "name": o.name} for o in ctx.db.scalars(select(model).order_by(model.name))]


def list_departments(ctx: Ctx) -> list[dict[str, Any]]:
    return _list_named(ctx, Department)


def create_department(ctx: Ctx, name: str) -> dict[str, Any]:
    return _create_named(ctx, Department, "department", name)


def bulk_create_departments(ctx: Ctx, names: list[str]) -> dict[str, Any]:
    return _bulk_create_named(ctx, Department, "department", names)


def rename_department(ctx: Ctx, dept_id: int, name: str) -> dict[str, Any]:
    return _rename(ctx, Department, "department", dept_id, name)


def delete_department(ctx: Ctx, dept_id: int) -> None:
    _delete_named(ctx, Department, "department", dept_id, Employee.department_id)


def list_designations(ctx: Ctx) -> list[dict[str, Any]]:
    return _list_named(ctx, Designation)


def create_designation(ctx: Ctx, name: str) -> dict[str, Any]:
    return _create_named(ctx, Designation, "designation", name)


def bulk_create_designations(ctx: Ctx, names: list[str]) -> dict[str, Any]:
    return _bulk_create_named(ctx, Designation, "designation", names)


def rename_designation(ctx: Ctx, des_id: int, name: str) -> dict[str, Any]:
    return _rename(ctx, Designation, "designation", des_id, name)


def delete_designation(ctx: Ctx, des_id: int) -> None:
    _delete_named(ctx, Designation, "designation", des_id, Employee.designation_id)


def find_department(ctx: Ctx, name: str) -> Department | None:
    """Case-insensitive exact match first, then 'contains' (used by CSV import and AI tools)."""
    needle = name.strip().lower()
    if not needle:
        return None
    exact = ctx.db.scalar(select(Department).where(func.lower(Department.name) == needle))
    if exact:
        return exact
    candidates = ctx.db.scalars(select(Department).where(func.lower(Department.name).contains(needle))).all()
    return candidates[0] if len(candidates) == 1 else None


# ------------------------------------------------------------------ leave types
def serialize_leave_type(t: LeaveType) -> dict[str, Any]:
    return {"id": t.id, "code": t.code, "name": t.name, "yearly_quota": t.yearly_quota, "is_paid": t.is_paid}


def list_leave_types(ctx: Ctx) -> list[dict[str, Any]]:
    require_authenticated(ctx)  # everyone needs leave types for the leave form
    return [serialize_leave_type(t) for t in ctx.db.scalars(select(LeaveType).order_by(LeaveType.id))]


def create_leave_type(ctx: Ctx, code: str, name: str, yearly_quota: int | None, is_paid: bool = True) -> dict[str, Any]:
    require_roles(ctx, Role.ADMIN)
    code = code.strip().lower()
    if not code or not name.strip():
        raise ValidationFailed("code and name are required")
    if yearly_quota is not None and yearly_quota < 0:
        raise ValidationFailed("yearly_quota must be >= 0 (or empty for unlimited)")
    if ctx.db.scalar(select(LeaveType).where(LeaveType.code == code)):
        raise Conflict(f"Leave type '{code}' already exists")
    t = LeaveType(code=code, name=name.strip(), yearly_quota=yearly_quota, is_paid=is_paid)
    ctx.db.add(t)
    ctx.db.flush()
    audit.record(ctx, "create", "leave_type", t.id, after=snapshot(t))
    return serialize_leave_type(t)


def update_leave_type(ctx: Ctx, type_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    require_roles(ctx, Role.ADMIN)
    t = ctx.db.get(LeaveType, type_id)
    if t is None:
        raise NotFound("Leave type not found")
    before = snapshot(t)
    if "name" in changes and changes["name"]:
        t.name = str(changes["name"]).strip()
    if "yearly_quota" in changes:
        q = changes["yearly_quota"]
        if q is not None and int(q) < 0:
            raise ValidationFailed("yearly_quota must be >= 0")
        t.yearly_quota = None if q is None else int(q)
    if "is_paid" in changes and changes["is_paid"] is not None:
        t.is_paid = bool(changes["is_paid"])
    audit.record(ctx, "update", "leave_type", t.id, before=before, after=snapshot(t))
    return serialize_leave_type(t)


def delete_leave_type(ctx: Ctx, type_id: int) -> None:
    require_roles(ctx, Role.ADMIN)
    t = ctx.db.get(LeaveType, type_id)
    if t is None:
        raise NotFound("Leave type not found")
    used = ctx.db.scalar(select(func.count(LeaveRequest.id)).where(LeaveRequest.leave_type_id == type_id)) or 0
    if used:
        raise Conflict("Leave type has leave requests and cannot be deleted")
    before = snapshot(t)
    ctx.db.delete(t)
    audit.record(ctx, "delete", "leave_type", type_id, before=before)


def create_standard_leave_types(ctx: Ctx) -> list[dict[str, Any]]:
    """Admin convenience: add the brief's standard policy (annual 14, sick 8, casual 10, unpaid)."""
    require_roles(ctx, Role.ADMIN)
    created = []
    for code, name, quota, paid in STANDARD_LEAVE_TYPES:
        if not ctx.db.scalar(select(LeaveType).where(LeaveType.code == code)):
            created.append(create_leave_type(ctx, code, name, quota, paid))
    return created


# ------------------------------------------------------------------ holidays
def list_holidays(ctx: Ctx, year: int | None = None) -> list[dict[str, Any]]:
    require_authenticated(ctx)
    stmt = select(Holiday).order_by(Holiday.day)
    if year:
        stmt = stmt.where(Holiday.day >= date(year, 1, 1), Holiday.day <= date(year, 12, 31))
    return [{"id": h.id, "date": h.day.isoformat(), "name": h.name} for h in ctx.db.scalars(stmt)]


def create_holiday(ctx: Ctx, day: date, name: str) -> dict[str, Any]:
    require_roles(ctx, Role.ADMIN)
    if not name.strip():
        raise ValidationFailed("Holiday name is required")
    if ctx.db.scalar(select(Holiday).where(Holiday.day == day)):
        raise Conflict(f"A holiday already exists on {day.isoformat()}")
    h = Holiday(day=day, name=name.strip())
    ctx.db.add(h)
    try:
        ctx.db.flush()
    except IntegrityError as exc:  # pragma: no cover - race condition guard
        raise Conflict("Holiday already exists") from exc
    audit.record(ctx, "create", "holiday", h.id, after=snapshot(h))
    return {"id": h.id, "date": h.day.isoformat(), "name": h.name}


def delete_holiday(ctx: Ctx, holiday_id: int) -> None:
    require_roles(ctx, Role.ADMIN)
    h = ctx.db.get(Holiday, holiday_id)
    if h is None:
        raise NotFound("Holiday not found")
    before = snapshot(h)
    ctx.db.delete(h)
    audit.record(ctx, "delete", "holiday", holiday_id, before=before)
