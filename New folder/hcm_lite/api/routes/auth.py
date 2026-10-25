"""M1 Auth + first-run setup."""

from fastapi import APIRouter, Depends, Request

from ...context import Ctx
from ...services import auth, org
from ..deps import commit, ctx_allow_password_change, public_ctx
from ..schemas import ChangePasswordIn, LoginIn, SetupIn

router = APIRouter(prefix="/api", tags=["auth & setup"])


@router.get("/setup/status")
def setup_status(ctx: Ctx = Depends(public_ctx)):
    return {"initialized": org.is_initialized(ctx)}


@router.post("/setup", status_code=201)
def first_run(body: SetupIn, ctx: Ctx = Depends(public_ctx)):
    result = org.first_run(ctx, company_name=body.company_name, admin_email=body.admin_email,
                           admin_password=body.admin_password, admin_name=body.admin_name,
                           create_standard_leave_types=body.create_standard_leave_types)
    return commit(ctx, result)


@router.post("/auth/login")
def login(body: LoginIn, request: Request, ctx: Ctx = Depends(public_ctx)):
    ip = request.client.host if request.client else "unknown"
    return commit(ctx, auth.login(ctx, body.email, body.password, ip=ip))


@router.get("/auth/me")
def me(ctx: Ctx = Depends(ctx_allow_password_change)):
    from ...models import User

    user = ctx.db.get(User, ctx.actor.user_id)
    return {**auth.serialize_user(user), "company": org.public_profile(ctx) if org.is_initialized(ctx) else None}


@router.post("/auth/change-password")
def change_password(body: ChangePasswordIn, ctx: Ctx = Depends(ctx_allow_password_change)):
    return commit(ctx, auth.change_password(ctx, body.current_password, body.new_password))


@router.post("/auth/logout", status_code=204)
def logout(everywhere: bool = False, ctx: Ctx = Depends(ctx_allow_password_change)):
    """Revokes this token on the server (or every session with ?everywhere=true)."""
    commit(ctx, auth.logout(ctx, everywhere=everywhere))
