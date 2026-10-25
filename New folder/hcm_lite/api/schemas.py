"""Pydantic request bodies (validation at the edge; business rules stay in services)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class SetupIn(BaseModel):
    company_name: str = Field(min_length=2, max_length=160)
    admin_name: str = Field(default="Administrator", max_length=160)
    admin_email: EmailStr
    admin_password: str = Field(min_length=8, max_length=128)
    create_standard_leave_types: bool = True


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class SettingsIn(BaseModel):
    name: str | None = None
    office_start: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    grace_minutes: int | None = None
    half_day_hours: float | None = None
    weekend_days: list[int] | None = None
    office_lat: float | None = None
    office_lng: float | None = None
    office_radius_m: int | None = None
    late_penalty_enabled: bool | None = None


class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class BulkNamesIn(BaseModel):
    names: list[str] = Field(min_length=1, max_length=500)


class LeaveTypeIn(BaseModel):
    code: str = Field(min_length=2, max_length=30)
    name: str = Field(min_length=2, max_length=80)
    yearly_quota: int | None = Field(default=None, ge=0, le=365)
    is_paid: bool = True


class LeaveTypePatch(BaseModel):
    name: str | None = None
    yearly_quota: int | None = Field(default=None, ge=0, le=365)
    is_paid: bool | None = None


class HolidayIn(BaseModel):
    date: date
    name: str = Field(min_length=2, max_length=120)


class EmployeeIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=160)
    email: EmailStr
    department_id: int
    designation_id: int | None = None
    join_date: date
    basic_salary: float | None = Field(default=None, gt=0)
    employee_code: str | None = Field(default=None, max_length=30)
    gender: Literal["male", "female", "other"] | None = None
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=300)
    status: Literal["active", "probation"] = "active"


class EmployeePatch(BaseModel):
    full_name: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    address: str | None = None
    gender: Literal["male", "female", "other"] | None = None
    department_id: int | None = None
    designation_id: int | None = None
    status: Literal["active", "probation"] | None = None
    join_date: date | None = None


class ProfileIn(BaseModel):
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=300)


class ExitIn(BaseModel):
    exit_date: date


class SalaryIn(BaseModel):
    basic: float = Field(gt=0)
    effective_from: date | None = None


class CameraCheckInIn(BaseModel):
    face_count: int = Field(ge=0, le=20)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(ge=0)


class CorrectionIn(BaseModel):
    date: date
    check_in: str = Field(pattern=r"^\d{2}:\d{2}$")
    check_out: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    reason: str = Field(min_length=5, max_length=500)


class DecisionIn(BaseModel):
    approve: bool
    comment: str | None = Field(default=None, max_length=500)


class LeaveIn(BaseModel):
    leave_type: str
    start: date
    end: date
    reason: str = Field(min_length=3, max_length=500)
    source: Literal["form", "smart"] = "form"


class PayrollRunIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")


class AnnouncementIn(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    body: str = Field(min_length=2, max_length=5000)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=2000)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=10)


class SummaryIn(BaseModel):
    dashboard: Literal["hr", "admin"] = "hr"
    date_from: date | None = None
    date_to: date | None = None
    department_id: int | None = None


class SmartLeaveIn(BaseModel):
    text: str = Field(min_length=4, max_length=500)
