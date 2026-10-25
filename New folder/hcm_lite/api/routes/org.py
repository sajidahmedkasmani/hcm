"""M2 Organization setup + Admin user management."""

from fastapi import APIRouter, Depends

from ...context import Ctx
from ...services import auth, org
from ...services.employees import EmployeeInput
from ..deps import commit, current_ctx
from ..schemas import BulkNamesIn, EmployeeIn, HolidayIn, LeaveTypeIn, LeaveTypePatch, NameIn, SettingsIn

router = APIRouter(prefix="/api", tags=["organization"])


@router.get("/settings")
def get_settings(ctx: Ctx = Depends(current_ctx)):
    return org.get_settings(ctx)


@router.get("/company")
def company_profile(ctx: Ctx = Depends(current_ctx)):
    return org.public_profile(ctx)


@router.put("/settings")
def update_settings(body: SettingsIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, org.update_settings(ctx, body.model_dump(exclude_unset=True)))


# departments / designations
@router.get("/departments")
def list_departments(ctx: Ctx = Depends(current_ctx)):
    return org.list_departments(ctx)


@router.post("/departments", status_code=201)
def create_department(body: NameIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, org.create_department(ctx, body.name))


@router.post("/departments/bulk")
def bulk_create_departments(body: BulkNamesIn, ctx: Ctx = Depends(current_ctx)):
    """Add many departments at once (one per list item). Existing names are skipped, not errors."""
    return commit(ctx, org.bulk_create_departments(ctx, body.names))


@router.put("/departments/{dept_id}")
def rename_department(dept_id: int, body: NameIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, org.rename_department(ctx, dept_id, body.name))


@router.delete("/departments/{dept_id}", status_code=204)
def delete_department(dept_id: int, ctx: Ctx = Depends(current_ctx)):
    commit(ctx, org.delete_department(ctx, dept_id))


@router.get("/designations")
def list_designations(ctx: Ctx = Depends(current_ctx)):
    return org.list_designations(ctx)


@router.post("/designations", status_code=201)
def create_designation(body: NameIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, org.create_designation(ctx, body.name))


@router.post("/designations/bulk")
def bulk_create_designations(body: BulkNamesIn, ctx: Ctx = Depends(current_ctx)):
    """Add many designations at once. Existing names are skipped, not errors."""
    return commit(ctx, org.bulk_create_designations(ctx, body.names))


@router.put("/designations/{des_id}")
def rename_designation(des_id: int, body: NameIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, org.rename_designation(ctx, des_id, body.name))


@router.delete("/designations/{des_id}", status_code=204)
def delete_designation(des_id: int, ctx: Ctx = Depends(current_ctx)):
    commit(ctx, org.delete_designation(ctx, des_id))


# leave types
@router.get("/leave-types")
def list_leave_types(ctx: Ctx = Depends(current_ctx)):
    return org.list_leave_types(ctx)


@router.post("/leave-types", status_code=201)
def create_leave_type(body: LeaveTypeIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, org.create_leave_type(ctx, body.code, body.name, body.yearly_quota, body.is_paid))


@router.post("/leave-types/standard", status_code=201)
def create_standard_leave_types(ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, org.create_standard_leave_types(ctx))


@router.patch("/leave-types/{type_id}")
def update_leave_type(type_id: int, body: LeaveTypePatch, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, org.update_leave_type(ctx, type_id, body.model_dump(exclude_unset=True)))


@router.delete("/leave-types/{type_id}", status_code=204)
def delete_leave_type(type_id: int, ctx: Ctx = Depends(current_ctx)):
    commit(ctx, org.delete_leave_type(ctx, type_id))


# holidays
@router.get("/holidays")
def list_holidays(year: int | None = None, ctx: Ctx = Depends(current_ctx)):
    return org.list_holidays(ctx, year)


@router.post("/holidays", status_code=201)
def create_holiday(body: HolidayIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, org.create_holiday(ctx, body.date, body.name))


@router.delete("/holidays/{holiday_id}", status_code=204)
def delete_holiday(holiday_id: int, ctx: Ctx = Depends(current_ctx)):
    commit(ctx, org.delete_holiday(ctx, holiday_id))


# users (Admin)
@router.get("/users")
def list_users(role: str | None = None, ctx: Ctx = Depends(current_ctx)):
    return auth.list_users(ctx, role)


@router.post("/users/hr", status_code=201)
def create_hr(body: EmployeeIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, auth.create_hr_user(ctx, EmployeeInput(**body.model_dump())))


@router.post("/users/{user_id}/deactivate")
def deactivate(user_id: int, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, auth.set_active(ctx, user_id, False))


@router.post("/users/{user_id}/activate")
def activate(user_id: int, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, auth.set_active(ctx, user_id, True))


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, auth.reset_password(ctx, user_id))
