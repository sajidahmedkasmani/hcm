"""Face enrollment for camera attendance.

* An employee enrolls their own face once (first use). Changing it afterwards needs HR (reset), so nobody
  can quietly swap the face on an account.
* HR can enroll an employee in person (and re-enroll after a reset).
* One face can't be enrolled on two accounts — this blocks one person checking in for a colleague.
Only a 128-number descriptor is stored, never a photo.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

from sqlalchemy import select

from ..context import Ctx, assert_can_view_employee, require_roles, require_self_employee
from ..errors import Conflict, NotFound, ValidationFailed
from ..models import Employee, EmploymentStatus, FaceProfile, Role
from . import audit, face

MIN_ENROLL_FRAMES, MAX_ENROLL_FRAMES = 3, 10


def enrolled_descriptor(ctx: Ctx, employee_id: int) -> list[float] | None:
    return ctx.db.scalar(select(FaceProfile.descriptor).where(FaceProfile.employee_id == employee_id))


def status(ctx: Ctx, employee_id: int | None = None) -> dict[str, Any]:
    employee_id = employee_id or require_self_employee(ctx)
    assert_can_view_employee(ctx, employee_id)
    profile = ctx.db.scalar(select(FaceProfile).where(FaceProfile.employee_id == employee_id))
    if profile is None:
        return {"employee_id": employee_id, "enrolled": False}
    return {"employee_id": employee_id, "enrolled": True, "method": profile.method, "samples": profile.samples,
            "enrolled_at": profile.enrolled_at.isoformat()}


def enroll(ctx: Ctx, frames: list[str], employee_id: int | None = None) -> dict[str, Any]:
    principal = ctx.actor
    by_hr = employee_id is not None and employee_id != principal.employee_id
    if by_hr:
        require_roles(ctx, Role.HR)
    else:
        employee_id = require_self_employee(ctx)
    employee = ctx.db.get(Employee, employee_id)
    if employee is None:
        raise NotFound("Employee not found")
    if employee.status == EmploymentStatus.EXITED:
        raise Conflict("This employee has exited")
    existing = ctx.db.scalar(select(FaceProfile).where(FaceProfile.employee_id == employee_id))
    if existing is not None and not by_hr:
        raise Conflict("Your face is already set up. Ask HR to reset it if it needs changing.",
                       code="face_already_enrolled")
    if not MIN_ENROLL_FRAMES <= len(frames) <= MAX_ENROLL_FRAMES:
        raise ValidationFailed(f"Send {MIN_ENROLL_FRAMES} to {MAX_ENROLL_FRAMES} camera frames")

    seen = face.inspect_frames(frames, max_descriptors=MAX_ENROLL_FRAMES)
    if len(seen.best_descriptors) < MIN_ENROLL_FRAMES:
        raise ValidationFailed("Keep your face in view for the whole capture", code="face_not_steady")
    spread = max(face.distance(a, b) for a, b in combinations(seen.best_descriptors, 2))
    if spread > face.ENROLL_CONSISTENCY:
        raise ValidationFailed("The frames didn't look like one steady face — hold still in good light and retry",
                               code="face_not_steady")
    descriptor = face.mean_descriptor(seen.best_descriptors)

    others = ctx.db.execute(
        select(FaceProfile.employee_id, FaceProfile.descriptor).where(FaceProfile.employee_id != employee_id)
    ).all()
    clash = next((emp for emp, d in others if face.distance(descriptor, d) <= ctx.settings.face_match_threshold), None)
    if clash is not None:
        audit.record(ctx, "reject", "face_profile", employee_id, after={"reason": "face_in_use", "other_employee": clash})
        ctx.db.commit()
        raise Conflict("This face is already enrolled for another employee", code="face_in_use")

    before = {"method": existing.method, "enrolled_at": existing.enrolled_at.isoformat()} if existing else None
    if existing is None:
        existing = FaceProfile(employee_id=employee_id)
        ctx.db.add(existing)
    existing.descriptor = descriptor
    existing.samples = len(seen.best_descriptors)
    existing.method = "hr" if by_hr else "self"
    existing.enrolled_by_user_id = principal.user_id
    existing.enrolled_at = ctx.now
    ctx.db.flush()
    audit.record(ctx, "enroll", "face_profile", employee_id, before=before,
                 after={"method": existing.method, "samples": existing.samples})  # never the descriptor itself
    return status(ctx, employee_id)


def reset(ctx: Ctx, employee_id: int) -> None:
    require_roles(ctx, Role.HR)
    profile = ctx.db.scalar(select(FaceProfile).where(FaceProfile.employee_id == employee_id))
    if profile is None:
        raise NotFound("No face is enrolled for this employee")
    before = {"method": profile.method, "enrolled_at": profile.enrolled_at.isoformat()}
    ctx.db.delete(profile)
    audit.record(ctx, "reset", "face_profile", employee_id, before=before)
