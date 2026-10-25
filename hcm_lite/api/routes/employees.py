"""M3 Employees + salary structure."""

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response

from ...context import Ctx
from ...services import employees, face_profiles, salary
from ...services.employees import EmployeeInput
from ..deps import commit, current_ctx
from ..schemas import EmployeeIn, EmployeePatch, ExitIn, FaceEnrollIn, ProfileIn, SalaryIn

router = APIRouter(prefix="/api", tags=["employees"])


@router.get("/employees")
def list_employees(
    q: str | None = None, department_id: int | None = None, designation_id: int | None = None,
    status: str | None = None, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=200),
    ctx: Ctx = Depends(current_ctx),
):
    return employees.list_employees(ctx, q=q, department_id=department_id, designation_id=designation_id,
                                    status=status, page=page, size=size)


@router.post("/employees", status_code=201)
def register_employee(body: EmployeeIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, employees.register_employee(ctx, EmployeeInput(**body.model_dump())))


@router.get("/employees/export.csv")
def export_csv(ctx: Ctx = Depends(current_ctx)):
    content = employees.export_csv(ctx)
    return Response(content, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="employees.csv"'})


@router.get("/employees/import-template.csv")
def import_template(ctx: Ctx = Depends(current_ctx)):
    return Response(employees.import_template(ctx), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="employee_import_template.csv"'})


@router.post("/employees/import")
async def import_employees(dry_run: bool = True, file: UploadFile = File(...), ctx: Ctx = Depends(current_ctx)):
    """CSV or Excel (.xlsx). Dry run returns row + field + reason for every problem; a real import is all-or-nothing."""
    raw = await file.read(employees.MAX_UPLOAD_BYTES + 1)
    text = employees.read_upload(file.filename or "", raw)
    return commit(ctx, employees.import_csv(ctx, text, dry_run=dry_run))


@router.get("/employees/{employee_id}")
def get_employee(employee_id: int, ctx: Ctx = Depends(current_ctx)):
    return employees.get_employee(ctx, employee_id)


@router.get("/employees/{employee_id}/360")
def employee_360(employee_id: int, ctx: Ctx = Depends(current_ctx)):
    return employees.employee_360(ctx, employee_id)


@router.patch("/employees/{employee_id}")
def update_employee(employee_id: int, body: EmployeePatch, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, employees.update_employee(ctx, employee_id, body.model_dump(exclude_unset=True)))


@router.post("/employees/{employee_id}/exit")
def exit_employee(employee_id: int, body: ExitIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, employees.exit_employee(ctx, employee_id, body.exit_date))


@router.get("/employees/{employee_id}/salary")
def get_salary(employee_id: int, ctx: Ctx = Depends(current_ctx)):
    return salary.get_salary(ctx, employee_id)


@router.put("/employees/{employee_id}/salary")
def set_salary(employee_id: int, body: SalaryIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, salary.set_salary(ctx, employee_id, body.basic, body.effective_from))


@router.get("/employees/{employee_id}/face")
def face_status(employee_id: int, ctx: Ctx = Depends(current_ctx)):
    return face_profiles.status(ctx, employee_id)


@router.post("/employees/{employee_id}/face", status_code=201)
def enroll_face_in_person(employee_id: int, body: FaceEnrollIn, ctx: Ctx = Depends(current_ctx)):
    """HR enrolls (or re-enrolls) an employee's face in person."""
    return commit(ctx, face_profiles.enroll(ctx, body.frames, employee_id))


@router.delete("/employees/{employee_id}/face", status_code=204)
def reset_face(employee_id: int, ctx: Ctx = Depends(current_ctx)):
    commit(ctx, face_profiles.reset(ctx, employee_id))


@router.get("/me/profile")
def my_profile(ctx: Ctx = Depends(current_ctx)):
    if ctx.actor.employee_id is None:
        return {"user": {"full_name": ctx.actor.full_name, "email": ctx.actor.email, "role": ctx.actor.role}}
    return employees.get_employee(ctx, ctx.actor.employee_id)


@router.patch("/me/profile")
def update_my_profile(body: ProfileIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, employees.update_own_profile(ctx, body.phone, body.address))
