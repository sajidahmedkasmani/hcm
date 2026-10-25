"""M9 — Announcements (Admin/HR post, everyone reads) and in-app notifications."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from ..context import Ctx, require_authenticated, require_roles
from ..errors import NotFound, ValidationFailed
from ..models import Announcement, Notification, Role, User
from . import audit


# ------------------------------------------------------------------ notifications
def notify(ctx: Ctx, user_id: int, kind: str, title: str, body: str) -> Notification:
    note = Notification(user_id=user_id, kind=kind, title=title, body=body, created_at=ctx.now)
    ctx.db.add(note)
    return note


def notify_role(ctx: Ctx, role: Role, kind: str, title: str, body: str, *, exclude_user_id: int | None = None) -> None:
    users = ctx.db.scalars(select(User).where(User.role == role, User.is_active.is_(True))).all()
    for user in users:
        if user.id != exclude_user_id:
            notify(ctx, user.id, kind, title, body)


def list_notifications(ctx: Ctx, *, unread_only: bool = False, limit: int = 50) -> dict[str, Any]:
    principal = ctx.actor
    stmt = select(Notification).where(Notification.user_id == principal.user_id)
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    rows = ctx.db.scalars(stmt.order_by(Notification.id.desc()).limit(limit)).all()
    unread = sum(1 for r in rows if not r.is_read)
    return {
        "unread": unread,
        "items": [
            {
                "id": n.id,
                "kind": n.kind,
                "title": n.title,
                "body": n.body,
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat(),
            }
            for n in rows
        ],
    }


def mark_read(ctx: Ctx, notification_id: int | None = None) -> None:
    principal = ctx.actor
    stmt = update(Notification).where(Notification.user_id == principal.user_id)
    if notification_id is not None:
        exists = ctx.db.scalar(
            select(Notification.id).where(
                Notification.id == notification_id, Notification.user_id == principal.user_id
            )
        )
        if not exists:
            raise NotFound("Notification not found")
        stmt = stmt.where(Notification.id == notification_id)
    ctx.db.execute(stmt.values(is_read=True))


# ------------------------------------------------------------------ announcements
def post_announcement(ctx: Ctx, title: str, body: str) -> dict[str, Any]:
    principal = require_roles(ctx, Role.ADMIN, Role.HR)
    if not title.strip() or not body.strip():
        raise ValidationFailed("Title and body are required")
    item = Announcement(title=title.strip(), body=body.strip(), author_user_id=principal.user_id, created_at=ctx.now)
    ctx.db.add(item)
    ctx.db.flush()
    audit.record(ctx, "create", "announcement", item.id, after={"title": item.title})
    return serialize_announcement(item, principal.full_name)


def list_announcements(ctx: Ctx, limit: int = 20) -> list[dict[str, Any]]:
    require_authenticated(ctx)
    rows = ctx.db.execute(
        select(Announcement, User.full_name)
        .join(User, User.id == Announcement.author_user_id)
        .order_by(Announcement.id.desc())
        .limit(limit)
    ).all()
    return [serialize_announcement(a, author) for a, author in rows]


def serialize_announcement(item: Announcement, author: str) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "body": item.body,
        "author": author,
        "created_at": item.created_at.isoformat(),
    }
