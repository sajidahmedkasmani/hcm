"""AI assistant: judges' live questions, role scoping, guardrails and the hosted-LLM tool loop."""

import json
from datetime import datetime

import httpx
from sqlalchemy import func, select

from hcm_lite.ai.offline import OfflineProvider
from hcm_lite.ai.providers import FallbackProvider, LLMResponse, OpenAICompatibleProvider, ProviderError
from hcm_lite.ai.service import AIService, ungrounded_numbers
from hcm_lite.models import AILog


def _activity(api, clock):
    """A few days of real activity: Sara late twice, Ali late once, Bilal sick leave approved."""
    for day, late in [(1, {"sara", "ali"}), (2, {"sara"})]:
        for who in ("ali", "sara", "bilal", "hr"):
            clock.set(datetime(2026, 10, day, 9, 40 if who in late else 9, 0))
            api.ok(api.post(who, "/api/attendance/check-in"), 201)
    clock.set(datetime(2026, 10, 2, 12, 0))
    req = api.ok(api.post("bilal", "/api/leaves", {"leave_type": "sick", "start": "2026-10-05", "end": "2026-10-06",
                                                   "reason": "Fever"}), 201)
    api.ok(api.post("hr", f"/api/leaves/{req['id']}/decision", {"approve": True}))
    clock.set(datetime(2026, 10, 5, 10, 0))


def ask(api, who, message):
    return api.ok(api.post(who, "/api/ai/chat", {"message": message}))


def test_judges_questions_offline(api, company, clock):
    _activity(api, clock)
    a = ask(api, "hr", "How many active employees are in Engineering?")
    assert "2 active employees in Engineering" in a["answer"] and a["tool_calls"][0]["tool"] == "get_headcount"
    a = ask(api, "hr", "Who came late most often this month?")
    assert a["answer"].startswith("Sara Ahmed came late most often (2 times")
    a = ask(api, "hr", "What is this month's attendance rate?")
    dash = api.ok(api.get("hr", "/api/dashboard/hr"))
    assert f"{dash['kpis']['attendance_rate_pct']}%" in a["answer"]  # same number as the dashboard
    a = ask(api, "hr", "Who is on leave today?")
    assert "1 employee on leave today (1 sick)" in a["answer"] and "Bilal Raza" in a["answer"]
    a = ask(api, "bilal", "How many sick leaves do I have left this year?")
    assert "6 of 8 sick leave day(s) left" in a["answer"]
    a = ask(api, "ali", "What is Sara's salary?")
    assert a["status"] == "refused" and "can't share" in a["answer"]
    for item in (a, ):
        assert item["label"] == "AI-generated"


def test_employee_cannot_reach_company_data_through_ai(api, company, clock):
    _activity(api, clock)
    a = ask(api, "ali", "How many employees are in Sales?")
    assert "can't share" in a["answer"] and a["tool_calls"] == []  # tool not even offered to Employees
    a = ask(api, "ali", "Who is on leave today?")
    assert "Bilal" not in a["answer"]  # employees only see their own leave
    a = ask(api, "ali", "How many times was I late this month?")
    assert "late 1 time(s)" in a["answer"]


def test_policy_qa_and_unknowns(api, company):
    a = ask(api, "ali", "What is the grace time for late arrival?")
    assert "09:15" in a["answer"] or "15-minute" in a["answer"]
    a = ask(api, "ali", "How is income tax calculated?")
    assert "600,000" in a["answer"]
    a = ask(api, "ali", "Can I bring my cat to the office?")
    assert "don't know" in a["answer"].lower() or "can answer" in a["answer"].lower()


def test_roman_urdu(api, company, clock):
    _activity(api, clock)
    a = ask(api, "bilal", "meri kitni chuttiyan baqi hain?")
    assert "baqi" in a["answer"]
    clock.set(datetime(2026, 10, 2, 10, 0))
    d = api.ok(api.post("ali", "/api/ai/smart-leave", {"text": "agle peer aur mangal ko chutti chahiye, shaadi hai"}))
    assert (d["draft"]["type"], d["draft"]["start"], d["draft"]["end"]) == ("casual", "2026-10-05", "2026-10-06")


def test_smart_leave_never_saves_and_previews_warnings(api, company, clock):
    clock.set(datetime(2026, 10, 2, 10, 0))
    d = api.ok(api.post("sara", "/api/ai/smart-leave", {"text": "I am sick, need off from 12 october to 23 october"}))
    assert d["draft"]["type"] == "sick" and d["preview"]["working_days"] == 10
    assert any("balance" in w.lower() or "available" in w.lower() for w in d["preview"]["warnings"])
    assert api.ok(api.get("sara", "/api/leaves"))["total"] == 0  # nothing was saved
    unclear = api.ok(api.post("sara", "/api/ai/smart-leave", {"text": "I need some time off soon please"}))
    assert unclear["fallback"] is True and unclear["draft"] is None


def test_rate_limit_and_ai_logs(api, company):
    for _ in range(20):
        ask(api, "ali", "What is the grace time?")
    resp = api.post("ali", "/api/ai/chat", {"message": "one more"})
    assert resp.status_code == 429
    session = api.client.app.state.hcm.session_factory()
    try:
        assert session.scalar(select(func.count(AILog.id))) == 20
    finally:
        session.close()


# ------------------------------------------------------------------ hosted LLM path (mocked HTTP)
def _mock_llm(script):
    """httpx transport that replays a scripted conversation and records requests."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"headers": dict(request.headers), "body": body})
        return httpx.Response(200, json={"choices": [{"message": script[len(seen) - 1](body)}]})

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_openai_compatible_tool_loop(api, company, clock):
    def step1(body):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "get_headcount", "arguments": json.dumps({"department": "Engineering"})}}]}

    def step2(body):
        tool_msg = body["messages"][-1]
        assert tool_msg["role"] == "tool" and json.loads(tool_msg["content"])["headcount"] == 2
        return {"role": "assistant", "content": "Engineering has 2 active employees."}

    client, seen = _mock_llm([step1, step2])
    settings = api.client.app.state.hcm.settings
    provider = OpenAICompatibleProvider(api_key="sk-test", base_url="https://llm.example/v1", model="m", client=client)
    api.client.app.state.hcm.ai = AIService(settings, provider=provider)
    a = ask(api, "hr", "How many people in engineering?")
    assert a["answer"] == "Engineering has 2 active employees." and a["provider"].startswith("openai-compatible")
    assert seen[0]["headers"]["authorization"] == "Bearer sk-test"
    offered = {t["function"]["name"] for t in seen[0]["body"]["tools"]}
    assert "get_headcount" in offered and "search_employees" in offered

    # Employees are never offered company-level tools
    client2, seen2 = _mock_llm([lambda b: {"role": "assistant", "content": "I can only help with your own data."}])
    api.client.app.state.hcm.ai = AIService(settings, provider=OpenAICompatibleProvider(
        api_key="k", base_url="https://llm.example/v1", model="m", client=client2))
    ask(api, "ali", "How many people in engineering?")
    offered = {t["function"]["name"] for t in seen2[0]["body"]["tools"]}
    assert offered == {"get_attendance_summary", "get_leave_summary", "get_my_leave_balance", "get_payroll_summary"}


def test_llm_tool_call_for_colleague_salary_is_blocked_server_side(api, company, clock):
    """Even if a model *tries* to fetch a colleague's pay, the tool's scope check refuses."""
    def step1(body):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "get_payroll_summary", "arguments": json.dumps({"employee": "Sara Ahmed"})}}]}

    def step2(body):
        assert json.loads(body["messages"][-1]["content"])["error"] == "forbidden"
        return {"role": "assistant", "content": "Sorry, I can't share a colleague's salary."}

    client, _ = _mock_llm([step1, step2])
    settings = api.client.app.state.hcm.settings
    api.client.app.state.hcm.ai = AIService(settings, provider=OpenAICompatibleProvider(
        api_key="k", base_url="https://llm.example/v1", model="m", client=client))
    a = ask(api, "ali", "What is Sara Ahmed's salary?")
    assert a["status"] == "refused" and a["tool_calls"][0]["error"] == "forbidden"


class _Broken:
    name = "broken"

    def complete(self, *args, **kwargs) -> LLMResponse:
        raise ProviderError("timeout")


def test_provider_failure_falls_back_gracefully(api, company):
    settings = api.client.app.state.hcm.settings
    api.client.app.state.hcm.ai = AIService(settings, provider=_Broken())
    a = ask(api, "ali", "What is the grace time?")
    assert a["status"] == "fallback" and "unavailable" in a["answer"]
    api.client.app.state.hcm.ai = AIService(settings, provider=FallbackProvider(_Broken(), OfflineProvider()))
    a = ask(api, "ali", "What is the grace time?")
    assert a["status"] == "ok" and a["provider"] == "offline-rules"


def test_summary_hallucination_guard(api, company, clock):
    calls = []

    class Liar:
        name = "liar"

        def complete(self, messages, **kwargs):
            calls.append(1)
            return LLMResponse(content=json.dumps({"bullets": ["Attendance hit 97.3% today.", "Morale is up 42%.",
                                                               "Everything is great."]}), provider="liar")

    settings = api.client.app.state.hcm.settings
    api.client.app.state.hcm.ai = AIService(settings, provider=Liar())
    out = api.ok(api.post("hr", "/api/ai/summary", {"dashboard": "hr"}))
    assert len(calls) == 2  # validated, retried once, then deterministic fallback
    assert out["fallback"] is True and 3 <= len(out["bullets"]) <= 5
    assert ungrounded_numbers(["Rate 97.3%"], {"kpis": {"rate": 97.3}}) == []
    assert ungrounded_numbers(["Rate 97%"], {"kpis": {"rate": 97.3}}) == []  # honest rounding is fine
    assert ungrounded_numbers(["Rate 88%"], {"kpis": {"rate": 97.3}}) == ["88"]
    assert ungrounded_numbers(["Hit 99.9% today"], {"kpis": {"dept_rate": 100.0}}) == ["99.9"]  # no rounding loophole


def test_large_tool_results_stay_valid_json():
    from hcm_lite.ai.tools import ToolRegistry

    big = {"total": 500, "items": [{"name": f"Employee {i}", "note": "x" * 200} for i in range(500)]}
    text = ToolRegistry.to_message(big, limit=6000)
    data = json.loads(text)  # must parse
    assert len(text) <= 6000 and (data.get("items_omitted") == 492 or data.get("truncated"))


def test_payroll_question_after_finalize(api, company, clock):
    clock.set(datetime(2026, 10, 31, 12, 0))
    run = api.ok(api.post("hr", "/api/payroll/runs", {"month": "2026-10"}), 201)
    api.ok(api.post("admin", f"/api/payroll/runs/{run['id']}/finalize"))
    a = ask(api, "hr", "Payroll summary for October")
    assert "Payroll 2026-10 (finalized): 4 employees" in a["answer"]
    a = ask(api, "hr", "What is Sara Ahmed's salary?")
    assert "Sara Ahmed's 2026-10 payslip (finalized)" in a["answer"]
    a = ask(api, "sara", "What is my salary?")
    assert "Your 2026-10 payslip: net" in a["answer"]
