"""Attendance and leave rules over HTTP."""

from datetime import datetime


def test_weekend_check_in_rejected_and_missing_checkout_derived(api, company, clock):
    clock.set(datetime(2026, 10, 3, 10, 0))  # Saturday
    assert api.post("ali", "/api/attendance/check-in").json()["error"] == "not_working_day"
    clock.set(datetime(2026, 10, 5, 9, 0))  # Monday
    api.ok(api.post("ali", "/api/attendance/check-in"), 201)
    clock.set(datetime(2026, 10, 6, 9, 0))  # next day, never checked out
    cal = api.ok(api.get("ali", "/api/attendance/calendar?month=2026-10"))
    day = next(d for d in cal["days"] if d["date"] == "2026-10-05")
    assert day["status"] == "missing_checkout"
    assert next(d for d in cal["days"] if d["date"] == "2026-10-02")["status"] == "absent"


def test_correction_request_and_hr_approval(api, company, clock):
    clock.set(datetime(2026, 10, 6, 10, 0))
    corr = api.ok(api.post("bilal", "/api/attendance/corrections",
                           {"date": "2026-10-05", "check_in": "09:05", "check_out": "17:30",
                            "reason": "Forgot to check in, was at a client site"}), 201)
    assert api.post("bilal", f"/api/attendance/corrections/{corr['id']}/decision", {"approve": True}).status_code == 403
    api.ok(api.post("hr", f"/api/attendance/corrections/{corr['id']}/decision", {"approve": True, "comment": "OK"}))
    board = api.ok(api.get("hr", "/api/attendance/board?day=2026-10-05"))
    row = next(r for r in board["rows"] if r["full_name"] == "Bilal Raza")
    assert (row["status"], row["source"]) == ("present", "correction")
    notes = api.ok(api.get("bilal", "/api/notifications"))
    assert notes["unread"] >= 1


def test_leave_rules_overlap_balance_weekend(api, company, clock):
    clock.set(datetime(2026, 10, 1, 10, 0))
    first = api.ok(api.post("sara", "/api/leaves", {"leave_type": "sick", "start": "2026-10-12", "end": "2026-10-16",
                                                    "reason": "Flu"}), 201)
    assert first["days"] == 5
    overlap = api.post("sara", "/api/leaves", {"leave_type": "casual", "start": "2026-10-16", "end": "2026-10-19",
                                               "reason": "Family"})
    assert overlap.status_code == 422 and overlap.json()["error"] == "leave_overlap"
    too_many = api.post("sara", "/api/leaves", {"leave_type": "sick", "start": "2026-10-19", "end": "2026-10-23",
                                                "reason": "Still sick"})
    assert too_many.status_code == 422 and too_many.json()["error"] == "insufficient_balance"  # 5 + 5 > 8
    weekend = api.post("sara", "/api/leaves", {"leave_type": "casual", "start": "2026-10-10", "end": "2026-10-11",
                                               "reason": "Weekend"})
    assert weekend.status_code == 422 and weekend.json()["error"] == "no_working_days"
    unpaid = api.ok(api.post("sara", "/api/leaves", {"leave_type": "unpaid", "start": "2026-11-02",
                                                     "end": "2026-11-27", "reason": "Long trip"}), 201)
    assert unpaid["days"] == 20  # unlimited quota


def test_balance_after_approval_and_cancel(api, company, clock):
    clock.set(datetime(2026, 10, 1, 10, 0))
    req = api.ok(api.post("ali", "/api/leaves", {"leave_type": "sick", "start": "2026-10-07", "end": "2026-10-08",
                                                 "reason": "Dentist"}), 201)
    bal = {b["leave_type"]: b for b in api.ok(api.get("ali", "/api/leaves/balance"))}
    assert (bal["sick"]["remaining"], bal["sick"]["pending"], bal["sick"]["available_to_apply"]) == (8, 2, 6)
    api.ok(api.post("hr", f"/api/leaves/{req['id']}/decision", {"approve": True}))
    bal = {b["leave_type"]: b for b in api.ok(api.get("ali", "/api/leaves/balance"))}
    assert (bal["sick"]["used"], bal["sick"]["remaining"]) == (2, 6)
    # approved but not started -> can cancel; balance restored
    api.ok(api.post("ali", f"/api/leaves/{req['id']}/cancel"))
    bal = {b["leave_type"]: b for b in api.ok(api.get("ali", "/api/leaves/balance"))}
    assert bal["sick"]["remaining"] == 8


def test_hr_cannot_approve_own_leave(api, company, clock):
    clock.set(datetime(2026, 10, 1, 10, 0))
    req = api.ok(api.post("hr", "/api/leaves", {"leave_type": "annual", "start": "2026-10-19", "end": "2026-10-20",
                                                "reason": "Trip"}), 201)
    assert api.post("hr", f"/api/leaves/{req['id']}/decision", {"approve": True}).status_code == 403


def test_check_in_blocked_on_approved_leave(api, company, clock):
    clock.set(datetime(2026, 10, 1, 10, 0))
    req = api.ok(api.post("bilal", "/api/leaves", {"leave_type": "casual", "start": "2026-10-02", "end": "2026-10-02",
                                                   "reason": "Errand"}), 201)
    api.ok(api.post("hr", f"/api/leaves/{req['id']}/decision", {"approve": True}))
    clock.set(datetime(2026, 10, 2, 9, 0))
    assert api.post("bilal", "/api/attendance/check-in").json()["error"] == "on_leave"


def test_absence_only_counts_after_registration(api, company, clock):
    """No login yet = no absence: tracking starts at the later of join date and registration."""
    clock.set(datetime(2026, 10, 7, 11, 0))  # HR registers someone mid-morning, with an older join date
    emp = api.ok(api.post("hr", "/api/employees", {"full_name": "Late Entry", "email": "late.entry@acme.pk",
                                                    "department_id": company["departments"]["sales"]["id"],
                                                    "join_date": "2026-09-01", "basic_salary": 50000}), 201)
    clock.set(datetime(2026, 10, 9, 12, 0))
    cal = api.ok(api.get("hr", f"/api/attendance/calendar?month=2026-10&employee_id={emp['id']}"))
    status = {d["date"]: d["status"] for d in cal["days"]}
    assert status["2026-10-06"] == "not_tracked" and status["2026-10-07"] == "not_tracked"
    assert status["2026-10-08"] == "absent" and status["2026-10-09"] == "not_checked_in"
