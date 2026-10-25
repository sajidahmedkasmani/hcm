"""M9 — Audit log: actor, action, entity, before/after JSON. Append-only; Admin can view."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from ..context import Ctx, require_roles
from ..models import AuditLog, Role
from .common import jsonable, paginate


def record(
    ctx: Ctx,
    action: str,
    entity: str,
    entity_id: Any = None,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    principal = ctx.principal
    entry = AuditLog(
        actor_user_id=principal.user_id if principal and principal.user_id else None,
        actor_role=principal.role.value if principal else "system",
        action=action,
        entity=entity,
        entity_id=str(entity_id) if entity_id is not None else None,
        before=jsonable(before) if before is not None else None,
        after=jsonable(after) if after is not None else None,
        created_at=ctx.now,
    )
    ctx.db.add(entry)
    return entry


def serialize(entry: AuditLog) -> dict[str, Any]:
    return {
        "id": entry.id,
        "actor_user_id": entry.actor_user_id,
        "actor_role": entry.actor_role,
        "action": entry.action,
        "entity": entry.entity,
        "entity_id": entry.entity_id,
        "before": entry.before,
        "after": entry.after,
        "created_at": entry.created_at.isoformat(),
    }


def list_entries(
    ctx: Ctx, *, entity: str | None = None, action: str | None = None, page: int = 1, size: int = 50
) -> dict[str, Any]:
    require_roles(ctx, Role.ADMIN)
    page, size = paginate(page, size)
    stmt = select(AuditLog)
    if entity:
        stmt = stmt.where(AuditLog.entity == entity)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    total = ctx.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = ctx.db.scalars(stmt.order_by(AuditLog.id.desc()).offset((page - 1) * size).limit(size)).all()
    return {"items": [serialize(r) for r in rows], "total": total, "page": page, "size": size}
