"""ORM models (tables). Business rules live in `services/`, not here."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, Money


# --------------------------------------------------------------------------- enums
class Role(StrEnum):
    ADMIN = "admin"
    HR = "hr"
    EMPLOYEE = "employee"


class EmploymentStatus(StrEnum):
    PROBATION = "probation"
    ACTIVE = "active"
    EXITED = "exited"


class AttendanceStatus(StrEnum):
    PRESENT = "present"
    LATE = "late"
    HALF_DAY = "half_day"
    MISSING_CHECKOUT = "missing_checkout"
    ABSENT = "absent"
    ON_LEAVE = "on_leave"
    HOLIDAY = "holiday"
    WEEKEND = "weekend"
    NOT_CHECKED_IN = "not_checked_in"
    UPCOMING = "upcoming"


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class PayrollStatus(StrEnum):
    DRAFT = "draft"
    FINALIZED = "finalized"


# --------------------------------------------------------------------------- org setup (M2)
class Company(Base):
    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    office_start: Mapped[str] = mapped_column(String(5), default="09:00")
    grace_minutes: Mapped[int] = mapped_column(Integer, default=15)
    half_day_hours: Mapped[float] = mapped_column(Float, default=5.0)
    weekend_days: Mapped[str] = mapped_column(String(20), default="5,6")  # Python weekday(): Sat=5, Sun=6
    office_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    office_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    office_radius_m: Mapped[int] = mapped_column(Integer, default=150)
    late_penalty_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(160))
    password_hash: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    employee: Mapped["Employee | None"] = relationship(back_populates="user", uselist=False)


class RevokedToken(Base):
    """Denylist of signed-out JWTs (by `jti`) until they would have expired anyway."""

    __tablename__ = "revoked_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC
    revoked_at: Mapped[datetime] = mapped_column(DateTime)


class LoginAttempt(Base):
    """Sign-in attempts, used for throttling (per email and per client IP)."""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(160), index=True)
    ip: Mapped[str] = mapped_column(String(64), index=True)
    success: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)


class Designation(Base):
    __tablename__ = "designations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)


class LeaveType(Base):
    __tablename__ = "leave_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    yearly_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unlimited
    is_paid: Mapped[bool] = mapped_column(Boolean, default=True)


class Holiday(Base):
    __tablename__ = "holidays"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, unique=True)
    name: Mapped[str] = mapped_column(String(120))


# --------------------------------------------------------------------------- employees (M3)
class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    employee_code: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(160), index=True)
    email: Mapped[str] = mapped_column(String(160), unique=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(20), nullable=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), index=True)
    designation_id: Mapped[int | None] = mapped_column(ForeignKey("designations.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=EmploymentStatus.ACTIVE)
    join_date: Mapped[date] = mapped_column(Date)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)

    user: Mapped[User] = relationship(back_populates="employee")
    department: Mapped[Department] = relationship()
    designation: Mapped[Designation | None] = relationship()


class SalaryStructure(Base):
    __tablename__ = "salary_structures"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    basic: Mapped[Decimal] = mapped_column(Money)
    hra: Mapped[Decimal] = mapped_column(Money)
    medical: Mapped[Decimal] = mapped_column(Money)
    gross: Mapped[Decimal] = mapped_column(Money)
    effective_from: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime)


# --------------------------------------------------------------------------- attendance (M4)
class AttendanceRecord(Base):
    __tablename__ = "attendance"
    __table_args__ = (UniqueConstraint("employee_id", "work_date", name="uq_attendance_emp_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    check_in: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    check_out: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)
    worked_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="web")  # web | camera | correction
    rules_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    geo: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class AttendanceCorrection(Base):
    __tablename__ = "attendance_corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    work_date: Mapped[date] = mapped_column(Date)
    requested_check_in: Mapped[datetime] = mapped_column(DateTime)
    requested_check_out: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default=RequestStatus.PENDING)
    reviewer_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# --------------------------------------------------------------------------- leave (M5)
class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    leave_type_id: Mapped[int] = mapped_column(ForeignKey("leave_types.id"))
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    days: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default=RequestStatus.PENDING, index=True)
    source: Mapped[str] = mapped_column(String(20), default="form")  # form | smart
    reviewer_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    leave_type: Mapped[LeaveType] = relationship()
    employee: Mapped[Employee] = relationship()


# --------------------------------------------------------------------------- payroll (M6)
class PayrollRun(Base):
    __tablename__ = "payroll_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), unique=True)  # YYYY-MM
    status: Mapped[str] = mapped_column(String(20), default=PayrollStatus.DRAFT)
    working_days: Mapped[int] = mapped_column(Integer)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    finalized_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    payslips: Mapped[list["Payslip"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Payslip(Base):
    __tablename__ = "payslips"
    __table_args__ = (UniqueConstraint("run_id", "employee_id", name="uq_payslip_run_emp"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("payroll_runs.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    basic: Mapped[Decimal] = mapped_column(Money)
    hra: Mapped[Decimal] = mapped_column(Money)
    medical: Mapped[Decimal] = mapped_column(Money)
    gross: Mapped[Decimal] = mapped_column(Money)  # full-month structure gross
    working_days: Mapped[int] = mapped_column(Integer)
    eligible_days: Mapped[int] = mapped_column(Integer)
    per_day: Mapped[Decimal] = mapped_column(Money)
    prorated_gross: Mapped[Decimal] = mapped_column(Money)
    absent_days: Mapped[float] = mapped_column(Float)
    unpaid_leave_days: Mapped[float] = mapped_column(Float)
    half_days: Mapped[float] = mapped_column(Float)
    late_count: Mapped[int] = mapped_column(Integer, default=0)
    late_penalty_days: Mapped[float] = mapped_column(Float, default=0.0)
    deduction_days: Mapped[float] = mapped_column(Float)
    deductions: Mapped[Decimal] = mapped_column(Money)
    taxable_annual: Mapped[Decimal] = mapped_column(Money)
    tax: Mapped[Decimal] = mapped_column(Money)
    net: Mapped[Decimal] = mapped_column(Money)

    run: Mapped[PayrollRun] = relationship(back_populates="payslips")
    employee: Mapped[Employee] = relationship()


# --------------------------------------------------------------------------- comms & audit (M9)
class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    author_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(1000))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_role: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(40), index=True)
    entity: Mapped[str] = mapped_column(String(60), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AILog(Base):
    __tablename__ = "ai_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    feature: Mapped[str] = mapped_column(String(30))  # chat | summary | smart_leave
    prompt: Mapped[str] = mapped_column(Text)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    provider: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20))  # ok | fallback | error | refused
    latency_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


# The audit log is append-only: block UPDATE and DELETE at the ORM level.
@event.listens_for(AuditLog, "before_update")
def _audit_no_update(*_args: Any) -> None:
    raise PermissionError("Audit log entries are immutable")


@event.listens_for(AuditLog, "before_delete")
def _audit_no_delete(*_args: Any) -> None:
    raise PermissionError("Audit log entries cannot be deleted")
