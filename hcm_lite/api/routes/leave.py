"""M5 Leave."""

from datetime import date

from fastapi import APIRouter, Depends

from ...context import Ctx
from ...services import leave
from ..deps import commit, current_ctx
from ..schemas import DecisionIn, LeaveIn

router = APIRouter(prefix="/api/leaves", tags=["leave"])


@router.get("")
def list_leaves(status: str | None = None, employee_id: int | None = None, department_id: int | None = None,
                date_from: date | None = None, date_to: date | None = None, ctx: Ctx = Depends(current_ctx)):
    return leave.list_leaves(ctx, status=status, employee_id=employee_id, department_id=department_id,
                             date_from=date_from, date_to=date_to)


@router.post("", status_code=201)
def apply_leave(body: LeaveIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, leave.apply_leave(ctx, leave_type=body.leave_type, start=body.start, end=body.end,
                                         reason=body.reason, source=body.source))


@router.get("/balance")
def balance(employee_id: int | None = None, year: int | None = None, ctx: Ctx = Depends(current_ctx)):
    return leave.balances(ctx, employee_id=employee_id, year=year)


@router.post("/{leave_id}/cancel")
def cancel(leave_id: int, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, leave.cancel_leave(ctx, leave_id))


@router.post("/{leave_id}/decision")
def decide(leave_id: int, body: DecisionIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, leave.decide_leave(ctx, leave_id, approve=body.approve, comment=body.comment))
