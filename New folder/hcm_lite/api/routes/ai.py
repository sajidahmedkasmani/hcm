"""M8 AI assistant endpoints. The browser talks only to these; the provider key never leaves the server."""

from fastapi import APIRouter, Depends

from ...ai.service import AIService
from ...context import Ctx
from ..deps import ai_service, commit, current_ctx
from ..schemas import ChatIn, SmartLeaveIn, SummaryIn

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.post("/chat")
def chat(body: ChatIn, ctx: Ctx = Depends(current_ctx), ai: AIService = Depends(ai_service)):
    history = [t.model_dump() for t in body.history]
    return commit(ctx, ai.chat(ctx, body.message, history))


@router.post("/summary")
def summary(body: SummaryIn, ctx: Ctx = Depends(current_ctx), ai: AIService = Depends(ai_service)):
    return commit(ctx, ai.summarize(ctx, dashboard=body.dashboard, date_from=body.date_from, date_to=body.date_to,
                                    department_id=body.department_id))


@router.post("/smart-leave")
def smart_leave(body: SmartLeaveIn, ctx: Ctx = Depends(current_ctx), ai: AIService = Depends(ai_service)):
    return commit(ctx, ai.smart_leave(ctx, body.text))


@router.get("/status")
def status(ctx: Ctx = Depends(current_ctx), ai: AIService = Depends(ai_service)):
    return {"provider": ai.provider.name, "rate_limit_per_hour": ai.settings.ai_rate_limit_per_hour,
            "timeout_seconds": ai.settings.ai_timeout_seconds}
