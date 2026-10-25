"""M4 Attendance."""

from datetime import date

from fastapi import APIRouter, Depends

from ...context import Ctx
from ...services import attendance
from ...services.common import month_key
from ..deps import commit, current_ctx
from ..schemas import CameraCheckInIn, CorrectionIn, DecisionIn

router = APIRouter(prefix="/api/attendance", tags=["attendance"])


@router.post("/check-in", status_code=201)
def check_in(ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, attendance.check_in(ctx, source="web"))


@router.post("/check-out")
def check_out(ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, attendance.check_out(ctx))


@router.post("/camera-check-in", status_code=201)
def camera_check_in(body: CameraCheckInIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, attendance.camera_check_in(ctx, face_count=body.face_count, lat=body.lat, lng=body.lng,
                                                  accuracy_m=body.accuracy_m))


@router.get("/today")
def today(ctx: Ctx = Depends(current_ctx)):
    return attendance.today_status(ctx)


@router.get("/calendar")
def calendar(month: str | None = None, employee_id: int | None = None, ctx: Ctx = Depends(current_ctx)):
    return attendance.calendar(ctx, month or month_key(ctx.today), employee_id)


@router.get("/board")
def board(day: date | None = None, department_id: int | None = None, ctx: Ctx = Depends(current_ctx)):
    return attendance.daily_board(ctx, day, department_id)


@router.get("/corrections")
def list_corrections(status: str | None = None, ctx: Ctx = Depends(current_ctx)):
    return attendance.list_corrections(ctx, status)


@router.post("/corrections", status_code=201)
def request_correction(body: CorrectionIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, attendance.request_correction(ctx, work_date=body.date, check_in=body.check_in,
                                                     check_out=body.check_out, reason=body.reason))


@router.post("/corrections/{correction_id}/decision")
def decide_correction(correction_id: int, body: DecisionIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, attendance.decide_correction(ctx, correction_id, approve=body.approve, comment=body.comment))
