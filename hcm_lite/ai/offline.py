"""Offline provider: a deterministic intent router that *speaks the same tool-calling protocol*
as a hosted LLM. It keeps the assistant useful with no API key, during provider outages, and
makes the whole agent loop testable.

  user msg  --route-->  ToolCall(name, args)  --agent executes-->  tool result  --compose-->  answer
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import date
from typing import Any

from . import nlu
from .providers import LLMResponse, ToolCall, new_call_id

POLICY_START, POLICY_END = "### COMPANY POLICY", "### END POLICY"

REFUSAL = {
    "en": "Sorry, I can't share that. You can only see your own records and salary — other employees' "
          "data is private.",
    "ur": "Maazrat, main yeh maloomat share nahi kar sakta. Aap sirf apna record aur apni tankhwah dekh sakte hain.",
}
HELP = {
    "en": "I can answer from live HR data: headcount, attendance rate, late-comers, who is on leave, your leave "
          "balance and payroll — plus company policy. Try: \"Who is on leave today?\" or \"How many sick leaves do I "
          "have left?\"",
    "ur": "Main headcount, haziri, late aane walon, chutti par kaun hai, aapki chutti ka balance aur payroll ke "
          "baare mein bata sakta hoon. Misal: \"Aaj chutti par kaun hai?\"",
}


def _fmt_money(v: float | int | None) -> str:
    return f"PKR {float(v or 0):,.0f}"


def _period_label(r: dict[str, Any]) -> str:
    f, t = r.get("from"), r.get("to")
    if f and t:
        return f if f == t else f"{f} to {t}"
    return "the selected period"


class OfflineProvider:
    name = "offline-rules"

    # ---------------------------------------------------------------- protocol entry point
    def complete(self, messages, *, tools=None, json_mode=False, timeout=15.0, metadata=None) -> LLMResponse:
        meta = metadata or {}
        task = meta.get("task", "chat")
        if task == "smart_leave":
            return LLMResponse(content=json.dumps(self._smart_leave(messages, meta)), provider=self.name)
        if task == "summary":
            return LLMResponse(content=json.dumps({"bullets": self._summary(meta.get("kpis") or {})}), provider=self.name)
        return self._chat(messages, tools or [], meta)

    # ---------------------------------------------------------------- chat
    def _chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], meta: dict[str, Any]) -> LLMResponse:
        user_text = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "") or ""
        lang = "ur" if nlu.is_roman_urdu(user_text) else "en"
        if messages and messages[-1]["role"] == "tool":
            return LLMResponse(content=self._compose(messages, lang, meta), provider=self.name)

        available = {t["function"]["name"] for t in tools}
        today = date.fromisoformat(meta["today"]) if meta.get("today") else date.today()
        route = self._route(user_text, today, meta)
        if route is None:
            policy_answer = self._policy_answer(messages, user_text)
            if policy_answer is None:
                prefix = "" if re.search(r"^\s*(hi|hello|hey|salam|assalam|aoa|help)\b", user_text.lower()) else (
                    "I don't know that from company policy or HR data. " if lang == "en" else "")
                return LLMResponse(content=prefix + HELP[lang], provider=self.name)
            return LLMResponse(content=policy_answer, provider=self.name)
        name, args = route
        if name == "__refuse__" or name not in available:
            text = REFUSAL[lang] if meta.get("role") == "employee" else (
                "That information isn't available to your role." if name != "__refuse__" else REFUSAL[lang])
            return LLMResponse(content=text, provider=self.name, refused=True)
        return LLMResponse(content=None, tool_calls=[ToolCall(new_call_id(), name, args)], provider=self.name)

    def _route(self, text: str, today: date, meta: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        t = nlu.normalize(text)
        pay_q = re.search(_PAY_RE, t)
        if pay_q and meta.get("role") == "employee" and re.search(_OTHERS_RE, t):
            return "__refuse__", {}  # aggregate / other-people pay questions, checked before anything else
        if re.search(_POLICY_RE, t):
            return None  # rule/policy question -> answered from the policy in the system prompt
        period = nlu.detect_period(t)
        dept = nlu.extract_department(text)
        is_self = bool(re.search(r"\b(my|i|me|mine|mera|meri|mere|mujhe|main)\b", t))

        if pay_q:
            if re.search(r"\b(policy|rule|how is|calculated|formula|tax slab|deduct)\b", t):
                return None  # payroll *policy* question -> policy answer
            person = nlu.extract_person(text)
            about_others = re.search(_OTHERS_RE, t)
            if person and not is_self:  # named colleague: let the tool's server-side scope check decide
                return "get_payroll_summary", {"employee": person}
            if meta.get("role") == "employee" and (about_others or dept):
                return "__refuse__", {}
            args: dict[str, Any] = {}
            month = nlu.detect_month(t, today)
            if month:
                args["month"] = month
            if dept:
                args["department"] = dept
            return "get_payroll_summary", args

        leave_words = r"\b(leave|leaves|chutti|chuttiyan|chhutti|chuttian|off)\b"
        if re.search(leave_words, t) and re.search(r"\b(left|remaining|balance|baqi|baki|bachi|available|have)\b", t) \
                and not re.search(r"\b(per year|quota|allowed|entitled|policy)\b", t):
            args = {}
            lt = next((c for c in ("sick", "casual", "annual", "unpaid") if c in t), None)
            if lt is None and re.search(r"\bbimar|bukhar\b", t):
                lt = "sick"
            if lt:
                args["leave_type"] = lt
            return "get_my_leave_balance", args

        if re.search(r"\bpending\b", t) and re.search(leave_words + r"|\brequests?\b|\bapprovals?\b", t):
            return "get_leave_summary", {"status": "pending"}

        if re.search(r"\bon leave\b|\bchutti par\b|\bchutti pe\b|\bleave today\b|\bwho is off\b|\bout of office\b", t):
            args = {"period": period or "today"}
            if dept:
                args["department"] = dept
            return "get_leave_summary", args

        if re.search(r"\b(late|der se|latecomers?|late-comers?|late comers?)\b", t):
            if is_self and meta.get("role") == "employee" or re.search(r"\b(was i|am i|did i|my)\b", t):
                return "get_attendance_summary", {"period": period or "this_month"}
            args = {"period": period or "this_month", "limit": 5}
            if dept:
                args["department"] = dept
            return "get_late_comers", args

        if re.search(r"\b(which|what)\s+(department|dept|team)s?\b", t) and \
                re.search(r"\b(absen\w*|attendance|late|haziri)\b", t):
            return "get_attendance_summary", {"period": period or "this_month", "group_by": "department"}

        if re.search(r"\b(attendance|present|absent|absences|absenteeism|haziri|hazri|rate)\b", t):
            args = {"period": period or "this_month"}
            if dept:
                args["department"] = dept
            return "get_attendance_summary", args

        if re.search(r"\b(how many|headcount|kitne|number of|total)\b", t) and \
                re.search(r"\b(employees?|staff|people|log|members?|headcount)\b", t):
            return "get_headcount", ({"department": dept} if dept else {})

        m = re.search(r"\b(?:find|search|look up|lookup|who is|details of|show)\s+(?:employee\s+)?([a-z][a-z .'-]{1,40})", t)
        if m and not re.search(r"\bon leave\b", t):
            return "search_employees", {"query": m[1].strip(" ?.")}
        if re.search(r"\b(list|show)\b.*\bemployees\b", t):
            return "search_employees", ({"department": dept} if dept else {})
        return None

    # ---------------------------------------------------------------- answers from tool results
    def _compose(self, messages: list[dict[str, Any]], lang: str, meta: dict[str, Any]) -> str:
        calls = {}
        for m in messages:
            for tc in m.get("tool_calls") or []:
                calls[tc["id"]] = (tc["function"]["name"], json.loads(tc["function"]["arguments"] or "{}"))
        results = [(calls.get(m.get("tool_call_id"), ("?", {})), json.loads(m["content"]))
                   for m in messages if m["role"] == "tool"]
        (name, args), result = results[-1]
        if isinstance(result, dict) and result.get("error"):
            if result["error"] == "forbidden":
                return REFUSAL[lang]
            return f"I couldn't get that: {result.get('message', 'unknown error')}"
        composer = getattr(self, f"_answer_{name}", None)
        return composer(args, result, lang, meta) if composer else json.dumps(result)[:600]

    def _answer_get_headcount(self, args, r, lang, meta) -> str:
        where = r.get("department") or "the company"
        n = r.get("headcount", 0)
        extra = ""
        if r.get("by_status"):
            parts = [f"{v} {k}" for k, v in r["by_status"].items() if v]
            extra = f" ({', '.join(parts)})" if parts else ""
        if lang == "ur":
            return f"{where} mein is waqt {n} active employees hain{extra}."
        noun = "employee" if n == 1 else "employees"
        verb = "is" if n == 1 else "are"
        return f"There {verb} {n} active {noun} in {where}{extra} as of {r.get('as_of')}."

    def _answer_get_attendance_summary(self, args, r, lang, meta) -> str:
        label = _period_label(r)
        if "departments" in r:
            rows = [d for d in r["departments"] if d["expected"]]
            if not rows:
                return "No attendance data for that period yet."
            top = rows[0]
            rest = ", ".join(f"{d['department']} {d['absenteeism_pct']}%" for d in rows[1:4])
            return (f"{top['department']} has the highest absenteeism for {label}: {top['absenteeism_pct']}% "
                    f"({top['absent']} absent of {top['expected']} expected days, attendance rate "
                    f"{top['attendance_rate_pct']}%)." + (f" Others: {rest}." if rest else ""))
        if meta.get("role") == "employee":
            if lang == "ur":
                return (f"{label}: aapki haziri {r['attendance_rate_pct']}% hai. Aap {r['late_arrivals']} dafa late "
                        f"aaye, {r['absent']} din absent aur {r['on_leave']} din chutti par rahe.")
            return (f"Your attendance for {label}: {r['attendance_rate_pct']}% — late {r['late_arrivals']} time(s), "
                    f"{r['half_day']} half day(s), {r['absent']} absent day(s), {r['on_leave']} day(s) on leave.")
        scope = f" in {args['department']}" if args.get("department") else ""
        if lang == "ur":
            return f"{label}{scope} haziri ki sharah {r['attendance_rate_pct']}% hai ({r['absent']} absent din)."
        return (f"Attendance rate{scope} for {label} is {r['attendance_rate_pct']}% across {r['employees']} employee(s): "
                f"{r['present']} on-time, {r['late']} late, {r['half_day']} half day, {r['missing_checkout']} missing "
                f"check-out, {r['absent']} absent and {r['on_leave']} on-leave day(s). Late arrivals: {r['late_pct']}%, "
                f"absenteeism: {r['absenteeism_pct']}%.")

    def _answer_get_late_comers(self, args, r, lang, meta) -> str:
        items = r.get("items", [])
        if not items:
            return "No late arrivals in that period." if lang == "en" else "Is dauran koi late nahi aaya."
        top = items[0]
        lines = ", ".join(f"{i['name']} ({i['late_count']})" for i in items)
        if lang == "ur":
            return f"Sab se zyada late {top['name']} aaye — {top['late_count']} dafa. Top list: {lines}."
        return (f"{top['name']} came late most often ({top['late_count']} times, {_period_label(r)}). "
                f"Top late-comers: {lines}.")

    def _answer_get_leave_summary(self, args, r, lang, meta) -> str:
        if "requests" in r:
            reqs = r["requests"]
            if not reqs:
                return "There are no pending leave requests."
            lines = "; ".join(f"{q['employee_name']}: {q['leave_type']} {q['start']}→{q['end']} ({q['days']}d)" for q in reqs[:8])
            return f"{len(reqs)} pending leave request(s): {lines}."
        n = r.get("count", 0)
        when = "today" if r.get("from") == r.get("to") == meta.get("today") else _period_label(r)
        if n == 0:
            return f"Nobody is on approved leave {when}." if lang == "en" else "Aaj koi chutti par nahi hai."
        types = ", ".join(f"{v} {k}" for k, v in sorted(r.get("by_type", {}).items(), key=lambda kv: -kv[1]))
        names = ", ".join(f"{e['name']} ({'/'.join(e['leave_types'])})" for e in r.get("employees", []))
        if lang == "ur":
            return f"{n} {'employee' if n == 1 else 'employees'} chutti par {'hai' if n == 1 else 'hain'} ({types}): {names}."
        noun = "employee" if n == 1 else "employees"
        return f"{n} {noun} on leave {when} ({types}): {names}."

    def _answer_get_my_leave_balance(self, args, r, lang, meta) -> str:
        rows = r.get("balances", [])
        if args.get("leave_type"):
            rows = [b for b in rows if b["leave_type"] == args["leave_type"]] or rows
        parts = []
        for b in rows:
            if b["quota"] is None:
                continue
            pend = f", {b['pending']} pending" if b["pending"] else ""
            if lang == "ur":
                parts.append(f"{b['name']}: {b['remaining']} din baqi ({b['quota']} mein se, {b['used']} istemal{pend})")
            else:
                parts.append(f"{b['remaining']} of {b['quota']} {b['name'].lower()} day(s) left "
                             f"({b['used']} used{pend})")
        year = rows[0]["year"] if rows else ""
        if not parts:
            return "No limited leave types found."
        if lang == "ur":
            return f"{year}: " + "; ".join(parts) + "."
        prefix = "You have " if len(parts) == 1 else f"Your {year} balances: "
        return prefix + "; ".join(parts) + (f" for {year}." if len(parts) == 1 else ".")

    def _answer_get_payroll_summary(self, args, r, lang, meta) -> str:
        if r.get("scope") == "own":
            p = r.get("payslip")
            if not p:
                return "You don't have a finalized payslip yet."
            return (f"Your {p['month']} payslip: net {_fmt_money(p['net'])} (gross {_fmt_money(p['prorated_gross'])}, "
                    f"attendance deductions {_fmt_money(p['deductions'])}, tax {_fmt_money(p['tax'])}).")
        run = r.get("run")
        if args.get("employee") and r.get("salary_structure"):
            st = r["salary_structure"]
            base = (f"{r.get('employee_name', args['employee'])}'s salary structure: basic {_fmt_money(st['basic'])}, "
                    f"gross {_fmt_money(st['gross'])} per month (HRA {_fmt_money(st['hra'])}, medical "
                    f"{_fmt_money(st['medical'])}).")
            if not run or not r.get("payslips"):
                return base + " No payslip has been generated yet."
        if not run:
            return r.get("message", "No payroll run found.")
        slips = r.get("payslips", [])
        if args.get("employee"):
            if not slips:
                return f"No payslip found for '{args['employee']}' in {run['month']}."
            p = slips[0]
            return (f"{p['employee_name']}'s {run['month']} payslip ({run['status']}): net {_fmt_money(p['net'])}, "
                    f"gross {_fmt_money(p['prorated_gross'])}, deductions {_fmt_money(p['deductions'])}, "
                    f"tax {_fmt_money(p['tax'])}.")
        tot = run["totals"]
        scope = f" for {run['department']}" if run.get("department") else ""
        depts = r.get("by_department") or {}
        top = ", ".join(f"{k} {_fmt_money(v['gross'])}" for k, v in
                        sorted(depts.items(), key=lambda kv: -kv[1]["gross"])[:3]) if not scope else ""
        return (f"Payroll {run['month']}{scope} ({run['status']}): {tot['employees']} employees, gross "
                f"{_fmt_money(tot['gross'])}, net {_fmt_money(tot['net'])}, tax {_fmt_money(tot['tax'])}."
                + (f" Largest departments by gross: {top}." if top else ""))

    def _answer_search_employees(self, args, r, lang, meta) -> str:
        items = r.get("items", [])
        if not items:
            return "No matching employees found."
        lines = "; ".join(
            f"{e['full_name']} ({e['employee_code']}, {e['department']}, {e.get('designation') or '—'}, {e['status']})"
            for e in items[:10]
        )
        return f"Found {r.get('total', len(items))}: {lines}."

    # ---------------------------------------------------------------- policy Q&A (lexical retrieval)
    @staticmethod
    def _policy_chunks(messages: list[dict[str, Any]]) -> list[str]:
        system = next((m["content"] for m in messages if m["role"] == "system"), "") or ""
        if POLICY_START not in system:
            return []
        body = system.split(POLICY_START, 1)[1].split(POLICY_END, 1)[0]
        return [line.strip("-• ").strip() for line in body.splitlines() if len(line.strip()) > 12]

    def _policy_answer(self, messages: list[dict[str, Any]], question: str) -> str | None:
        chunks = self._policy_chunks(messages)
        q_terms = [_stem(w) for w in nlu.tokens(question) if len(w) > 2 and w not in _STOPWORDS]
        if not chunks or not q_terms:
            return None
        df = Counter(w for c in chunks for w in {_stem(x) for x in nlu.tokens(c)})
        scored = []
        for c in chunks:  # binary term presence x IDF (saturated, so long lists of "leave" don't dominate)
            words = {_stem(x) for x in nlu.tokens(c)}
            score = sum(math.log(1 + len(chunks) / (1 + df[w])) for w in set(q_terms) if w in words)
            scored.append((score, c))
        scored.sort(reverse=True)
        best_score, best = scored[0]
        best_words = {_stem(x) for x in nlu.tokens(best)}
        coverage = sum(1 for w in set(q_terms) if w in best_words) / len(set(q_terms))
        if best_score < 0.8 or coverage < 0.5:
            return None
        extra = [c for s, c in scored[1:2] if s >= best_score * 0.7]
        return "According to company policy: " + " ".join([best, *extra])

    # ---------------------------------------------------------------- smart leave & summary
    def _smart_leave(self, messages: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
        text = meta.get("text") or next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        today = date.fromisoformat(meta["today"])
        weekend = frozenset(meta.get("weekend_days", [5, 6]))
        dates = nlu.parse_leave_dates(text, today, weekend)
        if dates is None:
            return {"error": "unclear_dates"}
        return {"type": nlu.detect_leave_type(text, meta.get("leave_types")), "start": dates[0].isoformat(),
                "end": dates[1].isoformat(), "reason": nlu.extract_reason(text)}

    @staticmethod
    def _summary(k: dict[str, Any]) -> list[str]:
        kp = k.get("kpis", {})
        bullets: list[str] = []
        if "attendance_rate_pct" in kp:
            rate = kp["attendance_rate_pct"]
            tone = "healthy" if rate >= 90 else "below the 90% comfort level"
            bullets.append(f"Attendance rate is {rate}% for the period — {tone}.")
        if kp.get("late_today") is not None and kp.get("present_today") is not None:
            bullets.append(f"Today {kp['present_today']} checked in, {kp['late_today']} late and "
                           f"{kp.get('on_leave_today', 0)} on leave; {kp.get('not_checked_in_today', 0)} not yet checked in.")
        depts = [d for d in k.get("absenteeism_by_department", []) if d.get("absenteeism_pct")]
        if depts:
            d = depts[0]
            bullets.append(f"Highest absenteeism: {d['department']} at {d['absenteeism_pct']}% ({d['absent']} absent days).")
        late = k.get("top_late_comers") or []
        if late:
            bullets.append(f"{late[0]['name']} leads late arrivals with {late[0]['late_count']} late days.")
        if kp.get("pending_approvals"):
            bullets.append(f"{kp['pending_approvals']} approval(s) are pending in the inbox.")
        if "headcount" in kp and "attrition_ytd_pct" in kp:
            bullets.append(f"Headcount is {kp['headcount']} with year-to-date attrition of {kp['attrition_ytd_pct']}% "
                           f"({kp.get('exits_ytd', 0)} exits).")
        if kp.get("payroll_cost_gross"):
            bullets.append(f"Payroll cost in range: gross PKR {kp['payroll_cost_gross']:,.0f}, net PKR "
                           f"{kp['payroll_cost_net']:,.0f}; average gross salary PKR {kp.get('average_salary_gross', 0):,.0f}.")
        leave = [x for x in k.get("leave_by_type", []) if x.get("approved_days")]
        if leave:
            top = max(leave, key=lambda x: x["approved_days"])
            bullets.append(f"Most used leave type: {top['name'].lower()} ({top['approved_days']} approved days).")
        if len(bullets) < 3:
            bullets.append(f"Headcount in scope: {kp.get('headcount', 0)}.")
        return bullets[:5]


_PAY_RE = (r"\b(salary|salaries|pay|paid|payslip|payroll|tankhwah|tankhah|net pay|gross|earns?|earning)\b"
           r"|how much does .+ (make|get)\b")
_OTHERS_RE = (r"\b(manager|boss|lead|colleagues?|co-?workers?|team|everyone|every|everybody|all|others?|highest|"
              r"lowest|most|least|top|average|avg|total|compare|sab|sabki|department|company)\b")
_POLICY_RE = (r"\b(policy|policies|rule|rules|what happens|allowed|grace|quota|quotas|entitled|formula|tax slab|"
              r"considered|counts? as|per year|calculated|how is .* (computed|calculated)|office hours|half[- ]day mean)\b")
_STOPWORDS = {"what", "the", "how", "does", "for", "and", "are", "is", "much", "many", "can", "you", "tell",
              "about", "our", "company", "policy", "rule", "rules", "get", "will", "when", "who", "which", "have",
              "happens", "happen", "come", "comes", "per", "if", "do", "there", "any", "that", "this",
              "with", "your", "mean", "means", "explain", "please"}


def _stem(word: str) -> str:
    w = word.lower().removesuffix("'s")
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("ly") and len(w) > 5:
        w = w[:-2]
    if w.endswith("ed") and len(w) > 5:
        w = w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        w = w[:-1]
    return w
