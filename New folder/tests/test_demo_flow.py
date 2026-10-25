"""End-to-end: the brief's 5-minute judging script, over HTTP, from a fresh install."""

from datetime import datetime

from hcm_lite.clock import FrozenClock


def test_judging_script_end_to_end(api, company, clock: FrozenClock):
    # Fresh install was set up by the fixture; setup cannot run twice
    assert api.post(None, "/api/setup", {"company_name": "Other Co", "admin_email": "x@x.pk",
                                         "admin_password": "Admin@12345"}).status_code == 409

    # Employee signs in (already changed password) and checks in — server stamps the time
    clock.set(datetime(2026, 10, 1, 9, 10))
    rec = api.ok(api.post("ali", "/api/attendance/check-in"), 201)
    assert rec["status"] == "present" and rec["check_in"] == "09:10"
    assert api.post("ali", "/api/attendance/check-in").status_code == 409  # one record per day

    clock.set(datetime(2026, 10, 1, 9, 40))
    assert api.ok(api.post("sara", "/api/attendance/check-in"), 201)["status"] == "late"

    clock.set(datetime(2026, 10, 1, 18, 0))
    assert api.ok(api.post("ali", "/api/attendance/check-out"))["status"] == "present"

    # Smart leave in plain words -> pre-filled form; employee confirms; HR approves
    clock.set(datetime(2026, 10, 2, 10, 0))  # Friday
    draft = api.ok(api.post("ali", "/api/ai/smart-leave",
                            {"text": "I need next Monday and Tuesday off for my sister's wedding"}))
    assert draft["fallback"] is False
    assert draft["draft"]["type"] == "casual"
    assert (draft["draft"]["start"], draft["draft"]["end"]) == ("2026-10-05", "2026-10-06")
    assert draft["label"] == "AI-generated"
    req = api.ok(api.post("ali", "/api/leaves", {"leave_type": draft["draft"]["type"], "start": draft["draft"]["start"],
                                                 "end": draft["draft"]["end"], "reason": draft["draft"]["reason"],
                                                 "source": "smart"}), 201)
    assert req["days"] == 2 and req["status"] == "pending"
    approved = api.ok(api.post("hr", f"/api/leaves/{req['id']}/decision", {"approve": True, "comment": "Enjoy!"}))
    assert approved["status"] == "approved"

    # HR dashboard + Summarize + "who is on leave"
    clock.set(datetime(2026, 10, 5, 11, 0))
    dash = api.ok(api.get("hr", "/api/dashboard/hr"))
    assert dash["kpis"]["on_leave_today"] == 1
    summary = api.ok(api.post("hr", "/api/ai/summary", {"dashboard": "hr"}))
    assert 3 <= len(summary["bullets"]) <= 5 and summary["label"] == "AI-generated"
    chat = api.ok(api.post("hr", "/api/ai/chat", {"message": "Who is on leave today?"}))
    assert "Ali Khan" in chat["answer"] and "(1 casual)" in chat["answer"]

    # HR runs payroll for the month; Admin finalizes it; edits after finalize are locked
    clock.set(datetime(2026, 10, 31, 12, 0))
    run = api.ok(api.post("hr", "/api/payroll/runs", {"month": "2026-10"}), 201)
    assert run["status"] == "draft" and run["totals"]["employees"] == 4
    assert api.post("hr", f"/api/payroll/runs/{run['id']}/finalize").status_code == 403  # HR cannot finalize
    fin = api.ok(api.post("admin", f"/api/payroll/runs/{run['id']}/finalize"))
    assert fin["status"] == "finalized"
    assert api.post("hr", "/api/payroll/runs", {"month": "2026-10"}).status_code == 409

    # Employee downloads own payslip; colleague's salary is refused (API and AI)
    slips = api.ok(api.get("ali", "/api/payslips"))
    assert len(slips) == 1 and slips[0]["employee_name"] == "Ali Khan"
    pdf = api.get("ali", f"/api/payslips/{slips[0]['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    sara_slip = [s for s in api.ok(api.get("hr", "/api/payslips")) if s["employee_name"] == "Sara Ahmed"][0]
    assert api.get("ali", f"/api/payslips/{sara_slip['id']}").status_code == 403
    refusal = api.ok(api.post("ali", "/api/ai/chat", {"message": "What is Sara Ahmed's salary?"}))
    assert "can't share" in refusal["answer"].lower() or "sorry" in refusal["answer"].lower()
    assert refusal["status"] == "refused"

    # Admin shows the audit log (HR cannot)
    audit = api.ok(api.get("admin", "/api/audit"))
    actions = {(e["action"], e["entity"]) for e in audit["items"]}
    assert ("finalize", "payroll_run") in actions and ("approve", "leave_request") in actions
    assert api.get("hr", "/api/audit").status_code == 403
