"""AI service: assistant chat (ReAct tool loop), dashboard summary, smart leave, with guardrails.

Guardrails (brief Section 4):
  * key only in backend env; the browser calls POST /ai/* on our server, never the provider
  * the model never queries the DB — tools reuse services + permission checks
  * the AI never saves data; JSON output is validated, retried once, then falls back
  * 15 s budget with friendly fallback, ~20 requests per user per hour, every call logged in ai_logs
  * every answer carries an 'AI-generated' label
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import func, select

from ..config import Settings
from ..context import Ctx, require_roles
from ..errors import RateLimited, ValidationFailed
from ..models import AILog, LeaveType, Role
from ..services import analytics, leave
from ..services.common import get_company, weekend_set
from .offline import POLICY_END, POLICY_START, OfflineProvider
from .policy import build_policy_text
from .providers import FallbackProvider, LLMProvider, OpenAICompatibleProvider, ProviderError
from .tools import MAX_RESULT_CHARS, ToolRegistry, build_registry

log = logging.getLogger("hcm_lite.ai")
AI_LABEL = "AI-generated"
FRIENDLY_FALLBACK = ("The assistant is unavailable right now. Please try again in a moment — "
                     "all numbers are also on your dashboard.")
SCOPE_TEXT = {
    Role.ADMIN: "Admin: may see all company data.",
    Role.HR: "HR: may see all employee HR data (people operations) but not company settings or the audit log.",
    Role.EMPLOYEE: "Employee: may see ONLY their own attendance, leave, balance and payslips. Never reveal "
                   "another employee's data or salary.",
}


def build_provider(settings: Settings) -> LLMProvider:
    offline = OfflineProvider()
    if not settings.ai_api_key:
        return offline
    primary = OpenAICompatibleProvider(api_key=settings.ai_api_key, base_url=settings.ai_base_url,
                                       model=settings.ai_model)
    return FallbackProvider(primary, offline) if settings.ai_offline_fallback else primary


# ------------------------------------------------------------------ structured outputs
class LeaveDraft(BaseModel):
    type: str
    start: date
    end: date
    reason: str = Field(min_length=2, max_length=300)

    @field_validator("type")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class SummaryOut(BaseModel):
    bullets: list[str] = Field(min_length=3, max_length=5)


_NUM_RE = re.compile(r"(?<![\w-])\d[\d,]*(?:\.\d+)?")


def _numbers_in(obj: Any) -> list[float]:
    if isinstance(obj, bool):
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, dict):
        return [n for v in obj.values() for n in _numbers_in(v)]
    if isinstance(obj, list):
        return [n for v in obj for n in _numbers_in(v)]
    return []


def ungrounded_numbers(bullets: list[str], kpis: dict[str, Any]) -> list[str]:
    """Hallucination guard: every number in the summary must exist in the KPI payload,
    at the precision it is written ('96.5' must match 96.5; '97' may round 96.5 or 97.2).
    Small integers 0-5 are allowed for phrasing such as 'top 5'."""
    values = _numbers_in(kpis)
    one_dp = {round(v, 1) for v in values}
    whole = {float(round(v)) for v in values} | {float(i) for i in range(0, 6)}
    bad = []
    for b in bullets:
        text = re.sub(r"\d{4}-\d{2}-\d{2}", " ", b)
        for raw in _NUM_RE.findall(text):
            value = float(raw.replace(",", ""))
            grounded = round(value, 1) in one_dp if "." in raw else (value in whole or round(value, 1) in one_dp)
            if not grounded:
                bad.append(raw)
    return bad


def _compact_kpis(payload: dict[str, Any]) -> dict[str, Any]:
    drop = {"announcements", "audit_feed"}
    data = {k: v for k, v in payload.items() if k not in drop}
    if "attendance_trend" in data:
        data["attendance_trend"] = data["attendance_trend"][-10:]
    return data


class AIService:
    def __init__(self, settings: Settings, provider: LLMProvider | None = None,
                 registry: ToolRegistry | None = None, max_steps: int = 4) -> None:
        self.settings = settings
        self.provider = provider or build_provider(settings)
        self.registry = registry or build_registry()
        self.max_steps = max_steps

    # -------------------------------------------------------------- guardrails
    def _check_rate_limit(self, ctx: Ctx) -> None:
        since = ctx.now - timedelta(hours=1)
        used = ctx.db.scalar(select(func.count(AILog.id)).where(AILog.user_id == ctx.actor.user_id,
                                                                AILog.created_at >= since)) or 0
        if used >= self.settings.ai_rate_limit_per_hour:
            oldest = ctx.db.scalar(select(func.min(AILog.created_at)).where(AILog.user_id == ctx.actor.user_id,
                                                                            AILog.created_at >= since))
            retry = max(1, int((oldest + timedelta(hours=1) - ctx.now).total_seconds())) if oldest else 3600
            raise RateLimited(f"AI limit reached ({self.settings.ai_rate_limit_per_hour} requests per hour). "
                              "Please try again later.", details={"retry_after_seconds": retry})

    def _provider_name(self) -> str:
        return getattr(self.provider, "last_used", None) or self.provider.name

    def _log(self, ctx: Ctx, feature: str, prompt: str, response: str | None, status: str,
             started: float, tool_calls: list[dict[str, Any]] | None = None) -> None:
        ctx.db.add(AILog(
            user_id=ctx.actor.user_id, role=ctx.actor.role.value, feature=feature, prompt=prompt[:4000],
            response=(response or "")[:4000], tool_calls=tool_calls, provider=self._provider_name(), status=status,
            latency_ms=int((time.monotonic() - started) * 1000), created_at=ctx.now,
        ))
        ctx.db.flush()

    def _remaining(self, started: float) -> float:
        return self.settings.ai_timeout_seconds - (time.monotonic() - started)

    def _complete(self, started: float, messages, **kwargs):
        remaining = self._remaining(started)
        if remaining <= 0.5:
            raise ProviderError("AI time budget exhausted")
        return self.provider.complete(messages, timeout=remaining, **kwargs)

    def _meta(self, ctx: Ctx, task: str, **extra: Any) -> dict[str, Any]:
        return {"task": task, "today": ctx.today.isoformat(), "role": ctx.actor.role.value, **extra}

    # -------------------------------------------------------------- chat (ReAct tool loop)
    def system_prompt(self, ctx: Ctx) -> str:
        p = ctx.actor
        company = get_company(ctx.db)
        return (
            f"You are the HCM Lite assistant for {company.name}. Today is {ctx.today.isoformat()} "
            f"({ctx.today.strftime('%A')}), time {ctx.now.strftime('%H:%M')}.\n"
            f"You are helping {p.full_name}. Role and data scope — {SCOPE_TEXT[p.role]}\n"
            "Rules:\n"
            "1. For ANY number about employees, attendance, leave or payroll call a tool. Never guess or invent numbers.\n"
            "2. Tools enforce the user's permissions. If a tool returns error 'forbidden', politely refuse and say "
            "the user can only see their own data.\n"
            "3. Answer policy questions ONLY from the COMPANY POLICY below. If it is not covered, say you don't know.\n"
            "4. You cannot create, approve or change anything. For leave requests point the user to Smart leave.\n"
            "5. Be concise: at most 5 sentences or a short list. Reply in the user's language (English or Roman Urdu).\n"
            f"{POLICY_START}\n{build_policy_text(ctx)}\n{POLICY_END}"
        )

    def chat(self, ctx: Ctx, message: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        message = (message or "").strip()
        if not message:
            raise ValidationFailed("Message is empty")
        if len(message) > 1000:
            raise ValidationFailed("Message is too long (max 1000 characters)")
        self._check_rate_limit(ctx)
        started = time.monotonic()
        tools = self.registry.specs_for(ctx.actor.role)
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt(ctx)}]
        for turn in (history or [])[-6:]:
            if turn.get("role") in ("user", "assistant") and turn.get("content"):
                messages.append({"role": turn["role"], "content": str(turn["content"])[:1000]})
        messages.append({"role": "user", "content": message})
        trace: list[dict[str, Any]] = []
        meta = self._meta(ctx, "chat")
        try:
            for _step in range(self.max_steps):
                resp = self._complete(started, messages, tools=tools, metadata=meta)
                if not resp.tool_calls:
                    answer = (resp.content or "").strip() or "I don't have an answer for that."
                    status = "refused" if resp.refused or any(t.get("error") == "forbidden" for t in trace) else "ok"
                    self._log(ctx, "chat", message, answer, status, started, trace)
                    return self._reply(answer, trace, status)
                messages.append({"role": "assistant", "content": resp.content or "",
                                 "tool_calls": [tc.to_message() for tc in resp.tool_calls]})
                for tc in resp.tool_calls:
                    result = self.registry.execute(ctx, tc.name, tc.arguments)
                    trace.append({"tool": tc.name, "arguments": tc.arguments,
                                  "ok": "error" not in result, "error": result.get("error")})
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": self.registry.to_message(result, MAX_RESULT_CHARS)})
            answer = "I couldn't finish that request in a few steps — please ask a more specific question."
            self._log(ctx, "chat", message, answer, "error", started, trace)
            return self._reply(answer, trace, "error")
        except ProviderError as exc:
            self._log(ctx, "chat", message, f"{FRIENDLY_FALLBACK} [{exc}]", "fallback", started, trace)
            return self._reply(FRIENDLY_FALLBACK, trace, "fallback")
        except Exception as exc:  # never surface a 500 from the assistant
            log.exception("assistant failure")
            ctx.db.rollback()
            self._log(ctx, "chat", message, f"error: {exc.__class__.__name__}", "error", started, trace)
            return self._reply(FRIENDLY_FALLBACK, trace, "error")

    def _reply(self, answer: str, trace: list[dict[str, Any]], status: str) -> dict[str, Any]:
        return {"answer": answer, "label": AI_LABEL, "provider": self._provider_name(), "status": status,
                "tool_calls": trace}

    # -------------------------------------------------------------- dashboard summary
    def dashboard_kpis(self, ctx: Ctx, dashboard: str, date_from: date | None, date_to: date | None,
                       department_id: int | None) -> dict[str, Any]:
        if dashboard == "admin":
            return analytics.admin_dashboard(ctx, date_from=date_from, date_to=date_to, department_id=department_id)
        return analytics.hr_dashboard(ctx, date_from=date_from, date_to=date_to, department_id=department_id)

    def summarize(self, ctx: Ctx, *, dashboard: str = "hr", date_from: date | None = None,
                  date_to: date | None = None, department_id: int | None = None) -> dict[str, Any]:
        """KPIs are recomputed on the server for the given filters (the DB is the only source of truth)."""
        require_roles(ctx, Role.ADMIN, Role.HR)
        if dashboard == "admin":
            require_roles(ctx, Role.ADMIN)
        self._check_rate_limit(ctx)
        started = time.monotonic()
        kpis = _compact_kpis(self.dashboard_kpis(ctx, dashboard, date_from, date_to, department_id))
        system = ("You turn HR dashboard KPI JSON into 3 to 5 short, useful insight bullets for an HR manager. "
                  "Use ONLY numbers that appear in the JSON — do not compute new numbers or percentages. "
                  'Respond with JSON only: {"bullets": ["...", "..."]}')
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(kpis, default=str)[:12000]}]
        meta = self._meta(ctx, "summary", kpis=kpis)
        error = ""
        for attempt in range(2):
            try:
                if attempt == 1:
                    messages.append({"role": "user", "content": f"Your previous answer was invalid: {error}. "
                                     "Return valid JSON with 3-5 bullets using only numbers from the data."})
                resp = self._complete(started, messages, json_mode=True, metadata=meta)
                out = SummaryOut.model_validate(json.loads(resp.content or "{}"))
                bad = ungrounded_numbers(out.bullets, kpis)
                if bad:
                    error = f"numbers not present in the data: {', '.join(bad[:5])}"
                    continue
                self._log(ctx, "summary", f"{dashboard} {kpis['filters']}", "\n".join(out.bullets), "ok", started)
                return {"bullets": out.bullets, "label": AI_LABEL, "provider": self._provider_name(),
                        "grounded": True, "fallback": False, "filters": kpis["filters"]}
            except (ValueError, ValidationError) as exc:
                error = str(exc)[:200]
            except ProviderError as exc:
                error = str(exc)
                break
        bullets = OfflineProvider._summary(kpis)  # deterministic, grounded by construction
        self._log(ctx, "summary", f"{dashboard} {kpis['filters']}", "\n".join(bullets), "fallback", started)
        return {"bullets": bullets, "label": AI_LABEL, "provider": "offline-rules", "grounded": True,
                "fallback": True, "filters": kpis["filters"]}

    # -------------------------------------------------------------- smart leave
    def smart_leave(self, ctx: Ctx, text: str) -> dict[str, Any]:
        """Plain words -> validated {type, start, end, reason}. Never saves: the user confirms the form."""
        principal = ctx.actor
        if principal.employee_id is None:
            raise ValidationFailed("Smart leave is for users with an employee profile")
        text = (text or "").strip()
        if len(text) < 4:
            raise ValidationFailed("Please describe your leave, e.g. 'Need next Monday and Tuesday off for a wedding'")
        self._check_rate_limit(ctx)
        started = time.monotonic()
        company = get_company(ctx.db)
        codes = list(ctx.db.scalars(select(LeaveType.code).order_by(LeaveType.id)))
        weekend = sorted(weekend_set(company))
        system = (
            f"Extract a leave request. Today is {ctx.today.isoformat()} ({ctx.today.strftime('%A')}). Weeks run "
            "Monday to Sunday; 'next <weekday>' means that weekday in the NEXT calendar week; a bare weekday means its "
            f"next occurrence after today. Leave types: {', '.join(codes)}. Use casual for personal or family events "
            "(weddings, functions), sick for illness, annual for vacations/travel, unpaid only if the user says unpaid. "
            "The user may write Roman Urdu (kal=tomorrow, parson=day after tomorrow, chutti=leave, shaadi=wedding). "
            'Respond with JSON only: {"type": "...", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "reason": "short reason"}. '
            'If the dates are unclear respond {"error": "unclear_dates"}.'
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": text}]
        meta = self._meta(ctx, "smart_leave", text=text, leave_types=codes, weekend_days=weekend)
        error = ""
        for attempt in range(2):
            try:
                if attempt == 1:
                    messages.append({"role": "user", "content": f"Invalid output ({error}). Return only the JSON object."})
                resp = self._complete(started, messages, json_mode=True, metadata=meta)
                raw = json.loads(resp.content or "{}")
                if raw.get("error"):
                    error = str(raw["error"])
                    break
                draft = LeaveDraft.model_validate(raw)
                if draft.type not in codes:
                    raise ValueError(f"type must be one of {codes}")
                if draft.end < draft.start:
                    raise ValueError("end before start")
                if not (ctx.today - timedelta(days=60) <= draft.start <= ctx.today + timedelta(days=366)):
                    raise ValueError("start date out of range")
                preview = self._preview(ctx, draft)
                self._log(ctx, "smart_leave", text, draft.model_dump_json(), "ok", started)
                return {"fallback": False, "draft": json.loads(draft.model_dump_json()), "preview": preview,
                        "label": AI_LABEL, "provider": self._provider_name(),
                        "message": "Please review and confirm — nothing is submitted until you press Apply."}
            except (ValueError, ValidationError) as exc:
                error = str(exc)[:200]
            except ProviderError as exc:
                error = str(exc)
                break
        self._log(ctx, "smart_leave", text, error, "fallback", started)
        return {"fallback": True, "draft": None, "label": AI_LABEL, "provider": self._provider_name(),
                "message": "I couldn't understand the dates. Please fill in the leave form."}

    @staticmethod
    def _preview(ctx: Ctx, draft: LeaveDraft) -> dict[str, Any]:
        days = leave.working_days_preview(ctx, draft.start, draft.end)
        bal = next((b for b in leave.balances(ctx, year=draft.start.year) if b["leave_type"] == draft.type), None)
        warnings = []
        if bal and bal["available_to_apply"] is not None and days > bal["available_to_apply"]:
            warnings.append(f"Only {bal['available_to_apply']} {draft.type} day(s) available.")
        try:
            t = leave.resolve_leave_type(ctx, draft.type)
            leave.validate_request(ctx, ctx.actor.employee_id, t, draft.start, draft.end)
        except ValidationFailed as exc:
            if exc.message not in " ".join(warnings):
                warnings.append(exc.message)
        return {"working_days": days, "balance": bal, "warnings": warnings}
