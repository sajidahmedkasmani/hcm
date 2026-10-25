"""Finding 5: payroll known-answer cases and end-to-end reconciliation.

Every expected figure below was computed by hand from the brief's rules (and cross-checked with plain
Decimal arithmetic outside the engine), then written as a literal — the engine is never its own oracle.

Rules: gross = 1.5 x basic; per day = gross / working days; prorate joiners/leavers by eligible working
days; deduct (absent + unpaid + 0.5 x half days [+ late penalty]) x per day; tax on prorated gross x 12
per the slab, /12, never more than pay left after deductions; amounts rounded half-up to paisa.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from hcm_lite.services.payroll import AttendanceFacts, calculate_payslip
from hcm_lite.services.salary import compute_structure
from tests.conftest import NEW_PASSWORD

D = Decimal

# id: (basic, working_days, eligible_days, facts, late_penalty) -> (prorated, deductions, tax, net)
KNOWN = {
    "full month":            ((100_000, 22, 22, AttendanceFacts(), False), ("150000.00", "0.00", "10000.00", "140000.00")),
    "2 unpaid days":         ((100_000, 22, 22, AttendanceFacts(unpaid_leave_days=2), False),
                              ("150000.00", "13636.36", "10000.00", "126363.64")),
    "1 absent + 3 half":     ((100_000, 22, 22, AttendanceFacts(absent_days=1, half_days=3), False),
                              ("150000.00", "17045.45", "10000.00", "122954.55")),
    "joiner 11 of 22":       ((100_000, 22, 11, AttendanceFacts(), False), ("75000.00", "0.00", "1250.00", "73750.00")),
    "leaver 7 of 21, 1 abs": ((60_000, 21, 7, AttendanceFacts(absent_days=1), False),
                              ("30000.00", "4285.71", "0.00", "25714.29")),
    "5% slab (720k/yr)":     ((40_000, 22, 22, AttendanceFacts(), False), ("60000.00", "0.00", "500.00", "59500.00")),
    "15% slab (1.44M/yr)":   ((80_000, 22, 22, AttendanceFacts(), False), ("120000.00", "0.00", "5500.00", "114500.00")),
    "exactly 3.6M/yr":       ((200_000, 22, 22, AttendanceFacts(), False), ("300000.00", "0.00", "42500.00", "257500.00")),
    "35% slab (5.4M/yr)":    ((300_000, 22, 22, AttendanceFacts(), False), ("450000.00", "0.00", "95000.00", "355000.00")),
    "zero net (all absent)": ((100_000, 22, 22, AttendanceFacts(absent_days=22), False),
                              ("150000.00", "150000.00", "0.00", "0.00")),
    "odd-paisa rounding":    ((33_333, 23, 23, AttendanceFacts(absent_days=1), False),
                              ("49999.50", "2173.89", "0.00", "47825.61")),
    "late penalty 6 lates":  ((100_000, 22, 22, AttendanceFacts(late_count=6), True),
                              ("150000.00", "6818.18", "10000.00", "133181.82")),
}


@pytest.mark.parametrize("case", KNOWN, ids=list(KNOWN))
def test_known_answer(case):
    (basic, wd, elig, facts, penalty), expected = KNOWN[case]
    calc = calculate_payslip(**compute_structure(basic), working_days=wd, eligible_days=elig, facts=facts,
                             late_penalty_enabled=penalty)
    assert (calc.prorated_gross, calc.deductions, calc.tax, calc.net) == tuple(D(x) for x in expected)
    assert calc.prorated_gross - calc.deductions - calc.tax == calc.net  # reconciles to the paisa
    assert all(v == v.quantize(D("0.01")) for v in (calc.deductions, calc.tax, calc.net))


# ------------------------------------------------------------------ end to end: November 2026
NOVEMBER = [d for d in (date(2026, 11, 1) + timedelta(days=i) for i in range(30))
            if d.weekday() < 5 and d != date(2026, 11, 9)]  # 20 working days: 21 weekdays minus Iqbal Day (Mon 9 Nov)

STAFF = {  # key: (name, basic, join date)
    "p1": ("Full Month", 100_000, "2025-01-01"),
    "p2": ("Paid Leave", 100_000, "2025-01-01"),
    "p3": ("Unpaid Leave", 100_000, "2025-01-01"),
    "p4": ("Absent Halfday", 100_000, "2025-01-01"),
    "p5": ("Mid Joiner", 60_000, "2026-11-16"),
    "p6": ("Mid Leaver", 80_000, "2024-01-01"),
}
# name: (working_days, eligible, absent, unpaid, half, deductions, tax, net) — by hand, per day = gross / 20
EXPECTED = {
    "Full Month":     (20, 20, 0, 0, 0, "0.00", "10000.00", "140000.00"),   # 150,000 - tax 120,000/12
    "Paid Leave":     (20, 20, 0, 0, 0, "0.00", "10000.00", "140000.00"),   # paid sick leave is not deducted
    "Unpaid Leave":   (20, 20, 0, 2, 0, "15000.00", "10000.00", "125000.00"),  # 2 x 7,500
    "Absent Halfday": (20, 20, 1, 0, 3, "18750.00", "10000.00", "121250.00"),  # (1 + 3 x 0.5) x 7,500
    "Mid Joiner":     (20, 11, 0, 0, 0, "0.00", "0.00", "49500.00"),        # 90,000 / 20 x 11; 594,000/yr is tax-free
    "Mid Leaver":     (20, 9, 1, 0, 0, "6000.00", "200.00", "47800.00"),    # 120,000/20 x 9 = 54,000; 5% of 48,000/yr = 2,400
    "Hina HR":        (20, 20, 0, 0, 0, "0.00", "16750.00", "178250.00"),   # 2.34M/yr: 30,000 + 15% x 1.14M = 201,000
}


def _at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm)


def test_november_payroll_end_to_end_reconciliation(api, clock):
    clock.set(datetime(2026, 10, 30, 10, 0))
    api.ok(api.post(None, "/api/setup", {"company_name": "Ledger Co", "admin_email": "admin@ledger.pk",
                                         "admin_password": "Admin@2026"}), 201)
    api.login("admin", "admin@ledger.pk", "Admin@2026")
    api.ok(api.put("admin", "/api/settings", {"attendance_mode": "web"}))  # payroll maths, not cameras
    ops = api.ok(api.post("admin", "/api/departments", {"name": "Operations"}), 201)["id"]
    api.ok(api.post("admin", "/api/holidays", {"date": "2026-11-09", "name": "Iqbal Day"}), 201)
    hr = api.ok(api.post("admin", "/api/users/hr", {"full_name": "Hina HR", "email": "hr@ledger.pk",
                                                    "department_id": ops, "join_date": "2025-01-01",
                                                    "basic_salary": 130_000}), 201)
    api.onboard("hr", "hr@ledger.pk", hr["temporary_password"])
    api.ok(api.put("hr", f"/api/employees/{hr['id']}/salary", {"basic": 130_000, "effective_from": "2025-01-01"}))
    ids = {}
    for key, (name, basic, join) in STAFF.items():
        e = api.ok(api.post("hr", "/api/employees", {"full_name": name, "email": f"{key}@ledger.pk",
                                                     "department_id": ops, "join_date": join,
                                                     "basic_salary": basic}), 201)
        api.onboard(key, f"{key}@ledger.pk", e["temporary_password"])
        ids[key] = e["id"]
    for key, ltype, start, end in [("p2", "sick", "2026-11-10", "2026-11-11"), ("p3", "unpaid", "2026-11-16", "2026-11-17")]:
        req = api.ok(api.post(key, "/api/leaves", {"leave_type": ltype, "start": start, "end": end, "reason": "Planned"}), 201)
        api.ok(api.post("hr", f"/api/leaves/{req['id']}/decision", {"approve": True}))

    leave_days = {"p2": {date(2026, 11, 10), date(2026, 11, 11)}, "p3": {date(2026, 11, 16), date(2026, 11, 17)}}
    absent = {"p4": {date(2026, 11, 3)}, "p6": {date(2026, 11, 12)}}
    half = {"p4": {date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)}}
    for d in NOVEMBER:
        for key in ["hr", *STAFF]:
            if key == "p5" and d < date(2026, 11, 16) or key == "p6" and d > date(2026, 11, 13):
                continue
            if d in leave_days.get(key, set()) or d in absent.get(key, set()):
                continue
            clock.set(_at(d, 9))
            api.ok(api.post(key, "/api/attendance/check-in"), 201)
            clock.set(_at(d, 12 if d in half.get(key, set()) else 18))
            api.ok(api.post(key, "/api/attendance/check-out"))
        if d == date(2026, 11, 13):
            clock.set(_at(d, 18, 30))
            api.ok(api.post("hr", f"/api/employees/{ids['p6']}/exit", {"exit_date": "2026-11-13"}))

    clock.set(datetime(2026, 12, 1, 10, 0))
    run = api.ok(api.post("hr", "/api/payroll/runs", {"month": "2026-11"}), 201)
    assert run["working_days"] == 20 and run["skipped"] == []
    slips = {p["employee_name"]: p for p in run["payslips"]}
    assert set(slips) == set(EXPECTED)
    for name, (wd, elig, absn, unpaid, halfd, ded, tax, net) in EXPECTED.items():
        p = slips[name]
        got = (p["working_days"], p["eligible_days"], p["absent_days"], p["unpaid_leave_days"], p["half_days"],
               D(str(p["deductions"])).quantize(D("0.01")), D(str(p["tax"])).quantize(D("0.01")),
               D(str(p["net"])).quantize(D("0.01")))
        assert got == (wd, elig, absn, unpaid, halfd, D(ded), D(tax), D(net)), name

    # --- reconciliation: payslips -> run totals -> dashboard -> employee view
    for p in run["payslips"]:
        assert round(p["prorated_gross"] - p["deductions"] - p["tax"], 2) == p["net"], p["employee_name"]
    t = run["totals"]
    assert t["employees"] == 7
    for field, src in (("gross", "prorated_gross"), ("deductions", "deductions"), ("tax", "tax"), ("net", "net")):
        assert round(sum(p[src] for p in run["payslips"]), 2) == round(t[field], 2), field
    assert round(t["gross"] - t["deductions"] - t["tax"], 2) == round(t["net"], 2)

    api.ok(api.post("admin", f"/api/payroll/runs/{run['id']}/finalize"))
    dash = api.ok(api.get("admin", "/api/dashboard/admin?date_from=2026-11-01&date_to=2026-11-30"))
    assert dash["kpis"]["payroll_cost_gross"] == round(t["gross"], 2)
    assert dash["kpis"]["payroll_cost_net"] == round(t["net"], 2)
    assert sum(d["gross"] for d in dash["payroll_cost_by_department"]) == pytest.approx(t["gross"], abs=0.01)
    own = api.ok(api.get("p3", "/api/payslips"))
    assert len(own) == 1 and own[0]["net"] == float(EXPECTED["Unpaid Leave"][7])
    ai = api.ok(api.post("admin", "/api/ai/chat", {"message": "Payroll summary for November"}))
    assert f"gross PKR {t['gross']:,.0f}" in ai["answer"]

    # --- locked after finalize: later changes don't alter stored payslips
    before = api.ok(api.get("hr", f"/api/payroll/runs/{run['id']}"))["payslips"]
    api.ok(api.put("hr", f"/api/employees/{ids['p1']}/salary", {"basic": 500_000, "effective_from": "2026-11-01"}))
    corr = api.ok(api.post("p4", "/api/attendance/corrections", {"date": "2026-11-03", "check_in": "09:00",
                                                                "check_out": "18:00", "reason": "Was at the bank site"}), 201)
    api.ok(api.post("hr", f"/api/attendance/corrections/{corr['id']}/decision", {"approve": True}))
    assert api.post("hr", "/api/payroll/runs", {"month": "2026-11"}).status_code == 409
    after = api.ok(api.get("hr", f"/api/payroll/runs/{run['id']}"))["payslips"]
    assert before == after


def test_salary_change_mid_month_uses_structure_effective_at_month_end(api, company, clock):
    """Documented simplification: one structure per payslip — the one in force on the last day of the month."""
    clock.set(datetime(2026, 10, 15, 10, 0))
    ali = company["people"]["ali"]["id"]
    api.ok(api.put("hr", f"/api/employees/{ali}/salary", {"basic": 120_000, "effective_from": "2026-10-15"}))
    clock.set(datetime(2026, 11, 2, 10, 0))
    run = api.ok(api.post("hr", "/api/payroll/runs", {"month": "2026-10"}), 201)
    slip = next(p for p in run["payslips"] if p["employee_id"] == ali)
    assert slip["basic"] == 120_000.0 and slip["gross"] == 180_000.0


def test_employee_sign_in_still_works(api, company):
    assert api.post(None, "/api/auth/login", {"email": "ali@acme.pk", "password": NEW_PASSWORD}).status_code == 200
