"""M7 Dashboards, analytics and bonus insights."""

from datetime import date

from fastapi import APIRouter, Depends

from ...context import Ctx
from ...services import analytics, insights
from ..deps import current_ctx

router = APIRouter(prefix="/api", tags=["dashboards"])


@router.get("/dashboard/hr")
def hr_dashboard(date_from: date | None = None, date_to: date | None = None, department_id: int | None = None,
                 ctx: Ctx = Depends(current_ctx)):
    return analytics.hr_dashboard(ctx, date_from=date_from, date_to=date_to, department_id=department_id)


@router.get("/dashboard/admin")
def admin_dashboard(date_from: date | None = None, date_to: date | None = None, department_id: int | None = None,
                    ctx: Ctx = Depends(current_ctx)):
    return analytics.admin_dashboard(ctx, date_from=date_from, date_to=date_to, department_id=department_id)


@router.get("/dashboard/employee")
def employee_dashboard(ctx: Ctx = Depends(current_ctx)):
    return analytics.employee_dashboard(ctx)


@router.get("/analytics/attendance")
def attendance_stats(date_from: date | None = None, date_to: date | None = None, period: str | None = None,
                     department_id: int | None = None, ctx: Ctx = Depends(current_ctx)):
    return analytics.attendance_stats(ctx, date_from=date_from, date_to=date_to, period=period,
                                      department_id=department_id)


@router.get("/analytics/leave-utilization")
def leave_utilization(year: int | None = None, department_id: int | None = None, ctx: Ctx = Depends(current_ctx)):
    from ...context import require_roles
    from ...models import Role

    require_roles(ctx, Role.ADMIN, Role.HR)
    return analytics.leave_utilization(ctx, year or ctx.today.year, department_id)


@router.get("/insights/attrition-risk")
def attrition_risk(lookback_days: int = 60, ctx: Ctx = Depends(current_ctx)):
    return insights.attrition_risk(ctx, lookback_days)


@router.get("/insights/absence-anomalies")
def absence_anomalies(lookback_days: int = 60, ctx: Ctx = Depends(current_ctx)):
    return insights.absence_anomalies(ctx, lookback_days)
