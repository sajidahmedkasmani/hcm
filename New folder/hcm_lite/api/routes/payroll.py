"""M6 Payroll Lite."""

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from ...context import Ctx
from ...services import payroll
from ..deps import commit, current_ctx
from ..schemas import PayrollRunIn

router = APIRouter(prefix="/api", tags=["payroll"])


@router.get("/payroll/runs")
def list_runs(ctx: Ctx = Depends(current_ctx)):
    return payroll.list_runs(ctx)


@router.post("/payroll/runs", status_code=201)
def create_or_refresh_draft(body: PayrollRunIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, payroll.create_or_refresh_draft(ctx, body.month))


@router.get("/payroll/runs/{run_id}")
def get_run(run_id: int, ctx: Ctx = Depends(current_ctx)):
    return payroll.get_run(ctx, run_id)


@router.post("/payroll/runs/{run_id}/finalize")
def finalize(run_id: int, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, payroll.finalize_run(ctx, run_id))


@router.get("/payslips")
def list_payslips(employee_id: int | None = None, month: str | None = None, ctx: Ctx = Depends(current_ctx)):
    return payroll.list_payslips(ctx, employee_id=employee_id, month=month)


@router.get("/payslips/{payslip_id}")
def get_payslip(payslip_id: int, ctx: Ctx = Depends(current_ctx)):
    return payroll.serialize_payslip(payroll.get_payslip_obj(ctx, payslip_id))


@router.get("/payslips/{payslip_id}/pdf")
def payslip_pdf(payslip_id: int, ctx: Ctx = Depends(current_ctx)):
    filename, content = payroll.payslip_pdf(ctx, payslip_id)
    return Response(content, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
