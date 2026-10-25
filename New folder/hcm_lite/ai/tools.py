"""Tool registry for the assistant. Each tool is a thin adapter over an existing service, so it
inherits the same RBAC and data-scope checks as the REST API. Tools are also *filtered by role*
before being offered to the model (defence in depth)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from sqlalchemy import func, select

from ..context import Ctx
from ..errors import DomainError, Forbidden, ValidationFailed
from ..models import Employee, Role
from ..services import analytics, employees, leave, org, salary
from ..services.common import PERIODS, jsonable, resolve_range

log = logging.getLogger("hcm_lite.ai.tools")
Handler = Callable[[Ctx, dict[str, Any]], dict[str, Any]]
ALL = frozenset({Role.ADMIN, Role.HR, Role.EMPLOYEE})
STAFF = frozenset({Role.ADMIN, Role.HR})
SELF_SERVICE = frozenset({Role.HR, Role.EMPLOYEE})
MAX_RESULT_CHARS = 6000


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    roles: frozenset[Role]
    handler: Handler

    def spec(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.name: t for t in tools}

    def names(self) -> list[str]:
        return list(self._tools)

    def for_role(self, role: Role) -> list[Tool]:
        return [t for t in self._tools.values() if role in t.roles]

    def specs_for(self, role: Role) -> list[dict[str, Any]]:
        return [t.spec() for t in self.for_role(role)]

    @staticmethod
    def to_message(result: dict[str, Any], limit: int = MAX_RESULT_CHARS) -> str:
        """Serialize a tool result for the model, shrinking long lists so the JSON stays valid."""
        text = json.dumps(result, default=str)
        if len(text) <= limit:
            return text
        slim: dict[str, Any] = {}
        for key, value in result.items():
            if isinstance(value, list) and len(value) > 8:
                slim[key] = value[:8]
                slim[f"{key}_omitted"] = len(value) - 8
            else:
                slim[key] = value
        text = json.dumps(slim, default=str)
        if len(text) <= limit:
            return text
        keep = {k: v for k, v in slim.items() if not isinstance(v, (list, dict))}
        return json.dumps({**keep, "truncated": True, "note": "Result too large; ask a narrower question."}, default=str)

    def execute(self, ctx: Ctx, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return {"error": "unknown_tool", "message": f"No tool named {name}"}
        if ctx.actor.role not in tool.roles:
            return {"error": "forbidden", "message": "This data is not available to your role"}
        if not isinstance(arguments, dict):
            return {"error": "validation_failed", "message": "Tool arguments must be a JSON object"}
        try:
            return jsonable(tool.handler(ctx, arguments))
        except DomainError as exc:
            return exc.to_dict()
        except (TypeError, ValueError) as exc:  # e.g. limit="lots" from a model
            return {"error": "validation_failed", "message": f"Invalid arguments for {name}: {exc}"}
        except Exception:  # never let one tool crash the conversation; the model can recover or apologise
            log.exception("tool %s failed", name)
            return {"error": "tool_failed", "message": f"{name} is temporarily unavailable"}


# ------------------------------------------------------------------ argument helpers
_PERIOD_PROPS: dict[str, Any] = {
    "period": {"type": "string", "enum": list(PERIODS), "description": "Relative period; default this_month"},
    "date_from": {"type": "string", "description": "ISO date YYYY-MM-DD (optional, overrides period)"},
    "date_to": {"type": "string", "description": "ISO date YYYY-MM-DD (optional)"},
}
_DEPT_PROP = {"department": {"type": "string", "description": "Department name, e.g. 'Engineering' (optional)"}}


def _date(args: dict[str, Any], key: str) -> date | None:
    value = args.get(key)
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValidationFailed(f"{key} must be YYYY-MM-DD") from exc


def _range(ctx: Ctx, args: dict[str, Any], default: str = "this_month") -> tuple[date, date]:
    return resolve_range(ctx.today, _date(args, "date_from"), _date(args, "date_to"), args.get("period") or default)


def _department_id(ctx: Ctx, args: dict[str, Any]) -> tuple[int | None, str | None]:
    name = (args.get("department") or "").strip()
    if not name:
        return None, None
    if not ctx.actor.sees_all_employees:
        return None, None  # employees are always scoped to themselves
    dept = org.find_department(ctx, name)
    if dept is None:
        known = ", ".join(d["name"] for d in org.list_departments(ctx)) or "none yet"
        raise ValidationFailed(f"Unknown department '{name}'. Known departments: {known}")
    return dept.id, dept.name


# ------------------------------------------------------------------ handlers
def get_headcount(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    dept_id, dept_name = _department_id(ctx, args)
    as_of = _date(args, "as_of") or ctx.today
    status = {}
    stmt = select(Employee.status, func.count(Employee.id)).where(analytics._employed_on_clause(as_of))
    if dept_id:
        stmt = stmt.where(Employee.department_id == dept_id)
    for st, n in ctx.db.execute(stmt.group_by(Employee.status)):
        status[st] = int(n)
    result = {"as_of": as_of.isoformat(), "department": dept_name,
              "headcount": analytics.headcount_on(ctx, as_of, dept_id), "by_status": status}
    if not dept_id:
        result["by_department"] = analytics.headcount_by(ctx, as_of, "department")
    return result


def get_attendance_summary(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    dept_id, _ = _department_id(ctx, args)
    start, end = _range(ctx, args)
    if args.get("group_by") == "department" and ctx.actor.sees_all_employees:
        rows = analytics.attendance_by_department(ctx, start, end, dept_id)
        return {"from": start.isoformat(), "to": end.isoformat(), "departments": rows}
    return analytics.attendance_stats(ctx, date_from=start, date_to=end, department_id=dept_id)


def get_late_comers(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    dept_id, _ = _department_id(ctx, args)
    start, end = _range(ctx, args)
    return analytics.top_late_comers(ctx, date_from=start, date_to=end, department_id=dept_id,
                                     limit=int(args.get("limit") or 5))


def get_leave_summary(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    dept_id, _ = _department_id(ctx, args)
    if (args.get("status") or "").lower() == "pending":
        items = leave.list_leaves(ctx, status="pending", department_id=dept_id)["items"]
        return {"status": "pending", "requests": items}
    if args.get("date"):
        start = end = _date(args, "date")
        assert start is not None and end is not None
    else:
        start, end = _range(ctx, args, default="today")
    return leave.who_is_on_leave(ctx, start, end, dept_id)


def get_my_leave_balance(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.actor.employee_id is None:
        raise ValidationFailed("Your account has no employee profile, so there is no leave balance")
    year = int(args["year"]) if args.get("year") else None
    rows = leave.balances(ctx, year=year)
    lt = (args.get("leave_type") or "").lower()
    if lt:
        rows = [r for r in rows if r["leave_type"] == lt] or rows
    return {"employee": ctx.actor.full_name, "balances": rows}


def get_payroll_summary(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    principal = ctx.actor
    target = (args.get("employee") or "").strip()
    if not principal.sees_all_employees:
        if target and target.lower() not in principal.full_name.lower():
            raise Forbidden("Employees can only view their own salary")
        return analytics.payroll_summary(ctx, month=args.get("month"))
    dept_id, _ = _department_id(ctx, args)
    if not target:
        return analytics.payroll_summary(ctx, month=args.get("month"), department_id=dept_id)
    found = employees.list_employees(ctx, q=target, size=5)["items"]
    if not found:
        raise ValidationFailed(f"No employee matches '{target}'")
    emp = found[0]
    result = analytics.payroll_summary(ctx, month=args.get("month"), department_id=dept_id, employee_id=emp["id"])
    result["employee_name"] = emp["full_name"]
    result["salary_structure"] = salary.get_salary(ctx, emp["id"])
    return result


def search_employees(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    dept_id, _ = _department_id(ctx, args)
    data = employees.list_employees(ctx, q=args.get("query") or None, department_id=dept_id,
                                    status=args.get("status") or None, size=int(args.get("limit") or 10))
    return {
        "total": data["total"],
        "items": [{"id": e["id"], "employee_code": e["employee_code"], "full_name": e["full_name"],
                   "email": e["email"], "department": e["department"]["name"],
                   "designation": e["designation"]["name"] if e["designation"] else None,
                   "status": e["status"], "join_date": e["join_date"]} for e in data["items"]],
    }


def build_registry() -> ToolRegistry:
    return ToolRegistry([
        Tool("get_headcount", "Number of active (active + probation) employees, optionally in one department.",
             {"type": "object", "properties": {**_DEPT_PROP, "as_of": {"type": "string", "description": "ISO date"}}},
             STAFF, get_headcount),
        Tool("get_attendance_summary",
             "Attendance KPIs for a period: attendance rate %, present/late/half-day/absent/on-leave day counts, "
             "late %, absenteeism %. Employees get only their own numbers.",
             {"type": "object", "properties": {**_PERIOD_PROPS, **_DEPT_PROP, "group_by": {
                 "type": "string", "enum": ["department"],
                 "description": "Set to 'department' to rank departments by absenteeism (HR/Admin)"}}},
             ALL, get_attendance_summary),
        Tool("get_late_comers", "Employees ranked by number of late arrivals in a period (top N).",
             {"type": "object", "properties": {**_PERIOD_PROPS, **_DEPT_PROP,
                                               "limit": {"type": "integer", "description": "Top N, default 5"}}},
             STAFF, get_late_comers),
        Tool("get_leave_summary",
             "Who is on approved leave on a date or period (default today), grouped by leave type; or pending leave "
             "requests when status='pending'. Employees only see their own leave.",
             {"type": "object", "properties": {"date": {"type": "string", "description": "ISO date"},
                                               **_PERIOD_PROPS, **_DEPT_PROP,
                                               "status": {"type": "string", "enum": ["approved", "pending"]}}},
             ALL, get_leave_summary),
        Tool("get_my_leave_balance", "The signed-in user's own leave balances (quota, used, pending, remaining).",
             {"type": "object", "properties": {
                 "leave_type": {"type": "string", "description": "annual, sick, casual or unpaid (optional)"},
                 "year": {"type": "integer"}}},
             SELF_SERVICE, get_my_leave_balance),
        Tool("get_payroll_summary",
             "Payroll totals for a month (gross, net, tax) and payslips. HR/Admin may filter by department or "
             "employee name. Employees only get their own latest payslip.",
             {"type": "object", "properties": {"month": {"type": "string", "description": "YYYY-MM"},
                                               **_DEPT_PROP,
                                               "employee": {"type": "string", "description": "Employee name"}}},
             ALL, get_payroll_summary),
        Tool("search_employees", "Search the employee directory by name, code or email; filter by department/status.",
             {"type": "object", "properties": {"query": {"type": "string"}, **_DEPT_PROP,
                                               "status": {"type": "string", "enum": ["active", "probation", "exited"]},
                                               "limit": {"type": "integer"}}},
             STAFF, search_employees),
    ])
