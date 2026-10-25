"""M9 Announcements, notifications, audit log."""

from fastapi import APIRouter, Depends

from ...context import Ctx
from ...services import audit, comms
from ..deps import commit, current_ctx
from ..schemas import AnnouncementIn

router = APIRouter(prefix="/api", tags=["comms & audit"])


@router.get("/announcements")
def list_announcements(ctx: Ctx = Depends(current_ctx)):
    return comms.list_announcements(ctx)


@router.post("/announcements", status_code=201)
def post_announcement(body: AnnouncementIn, ctx: Ctx = Depends(current_ctx)):
    return commit(ctx, comms.post_announcement(ctx, body.title, body.body))


@router.get("/notifications")
def list_notifications(unread_only: bool = False, ctx: Ctx = Depends(current_ctx)):
    return comms.list_notifications(ctx, unread_only=unread_only)


@router.post("/notifications/read-all", status_code=204)
def read_all(ctx: Ctx = Depends(current_ctx)):
    commit(ctx, comms.mark_read(ctx))


@router.post("/notifications/{notification_id}/read", status_code=204)
def read_one(notification_id: int, ctx: Ctx = Depends(current_ctx)):
    commit(ctx, comms.mark_read(ctx, notification_id))


@router.get("/audit")
def audit_log(entity: str | None = None, action: str | None = None, page: int = 1, size: int = 50,
              ctx: Ctx = Depends(current_ctx)):
    return audit.list_entries(ctx, entity=entity, action=action, page=page, size=size)
