"""Payroll from real attendance, CSV import, and dashboard numbers matching the records."""

from datetime import date, datetime, timedelta

from hcm_lite.services.common import WorkCalendar


def _work_days(start: date, end: date) -> list[date]:
    return WorkCalendar(frozenset({5, 6}), {}).working_days(start, end)


def test_payroll_uses_attendance_and_prorates_joiner(api, company, clock):
    eng = company["departments"]["eng"]["id"]
    clock.set(datetime(2026, 10, 1, 8, 0))
    joiner = api.ok(api.post("hr", "/api/employees", {"full_name": "Zara Joiner", "email": "zara@acme.pk",
                                                       "department_id": eng, "join_date": "2026-10-12",
                                                       "basic_salary": 100000}), 201)
    api.onboard("zara", "zara@acme.pk", joiner["temporary_password"])
    # Ali: present every working day except Oct 7 (absent) and Oct 8 (half day)
    for d in _work_days(date(2026, 10, 1), date(2026, 10, 30)):
        if d == date(2026, 10, 7):
            continue
        clock.set(datetime.combine(d, datetime.min.time()).replace(hour=9))
        api.ok(api.post("ali", "/api/attendance/check-in"), 201)
        clock.set(clock.now() + timedelta(hours=3 if d == date(2026, 10, 8) else 9))
        api.ok(api.post("ali", "/api/attendance/check-out"))
        if d >= date(2026, 10, 12):
            clock.set(datetime.combine(d, datetime.min.time()).replace(hour=9))
            api.ok(api.post("zara", "/api/attendance/check-in"), 201)
            clock.set(clock.now() + timedelta(hours=9))
            api.ok(api.post("zara", "/api/attendance/check-out"))
    clock.set(datetime(2026, 10, 31, 10, 0))
    run = api.ok(api.post("hr", "/api/payroll/runs", {"month": "2026-10"}), 201)
    slips = {p["employee_name"]: p for p in run["payslips"]}
    ali = slips["Ali Khan"]
    assert (ali["working_days"], ali["absent_days"], ali["half_days"], ali["deduction_days"]) == (22, 1, 1, 1.5)
    assert ali["deductions"] == round(150000 / 22 * 1.5, 2)
    assert ali["tax"] == 10000.0 and ali["net"] == round(150000 - 150000 / 22 * 1.5 - 10000, 2)
    zara = slips["Zara Joiner"]
    assert zara["eligible_days"] == 15 and zara["prorated_gross"] == 102272.73 and zara["absent_days"] == 0
    # Employees can't see draft payslips; recalculation allowed until finalized
    assert api.ok(api.get("ali", "/api/payslips")) == []
    api.ok(api.post("hr", "/api/payroll/runs", {"month": "2026-10"}), 201)
    api.ok(api.post("admin", f"/api/payroll/runs/{run['id']}/finalize"))
    assert len(api.ok(api.get("ali", "/api/payslips"))) == 1
    assert api.post("hr", "/api/payroll/runs", {"month": "2026-11"}).status_code == 422  # future month


def test_csv_import_dry_run_reports_row_field_reason(api, company):
    csv_text = (
        "employee_code,full_name,email,department,designation,join_date,basic_salary\n"
        "E-100,Good Person,good@acme.pk,Engineering,Developer,2026-09-01,70000\n"
        "E-101,Bad Email,not-an-email,Engineering,,2026-09-01,70000\n"
        "E-100,Dup Code,dup@acme.pk,Marketing,,01/09/2026,-5\n"
        ",,ali@acme.pk,Sales,,2026-09-01,50000\n"
    )
    files = {"file": ("staff.csv", csv_text, "text/csv")}
    report = api.ok(api.client.post("/api/employees/import?dry_run=true", files=files,
                                    headers={"Authorization": f"Bearer {api.tokens['hr']}"}))
    errors = {(e["row"], e["field"], e["reason"]) for e in report["errors"]}
    assert (3, "email", "invalid email") in errors
    assert (4, "employee_code", "duplicate in file") in errors
    assert (4, "department", "unknown department 'Marketing'") in errors
    assert (4, "join_date", "must be an ISO date (YYYY-MM-DD)") in errors
    assert (4, "basic_salary", "must be greater than 0") in errors
    assert (5, "full_name", "required") in errors and (5, "email", "already registered") in errors
    assert report["valid_rows"] == 1
    # real import is atomic: refused while errors exist
    bad = api.client.post("/api/employees/import?dry_run=false", files=files,
                          headers={"Authorization": f"Bearer {api.tokens['hr']}"})
    assert bad.status_code == 422
    good_csv = "full_name,email,department,join_date,basic_salary\nGood Person,good@acme.pk,Engineering,2026-09-01,70000\n"
    done = api.ok(api.client.post("/api/employees/import?dry_run=false",
                                  files={"file": ("ok.csv", good_csv, "text/csv")},
                                  headers={"Authorization": f"Bearer {api.tokens['hr']}"}))
    assert len(done["created"]) == 1 and done["created"][0]["temporary_password"]
    export = api.get("hr", "/api/employees/export.csv")
    assert export.status_code == 200 and "good@acme.pk" in export.text


def test_dashboard_numbers_match_records(api, company, clock):
    """Attendance rate formula: (present + late + missing + 0.5 x half) / (working-day records - on_leave)."""
    # Oct 1 (Thu): ali on time, sara late, bilal absent, hina half day
    clock.set(datetime(2026, 10, 1, 9, 0))
    api.ok(api.post("ali", "/api/attendance/check-in"), 201)
    api.ok(api.post("hr", "/api/attendance/check-in"), 201)
    clock.set(datetime(2026, 10, 1, 9, 45))
    api.ok(api.post("sara", "/api/attendance/check-in"), 201)
    clock.set(datetime(2026, 10, 1, 12, 0))
    api.ok(api.post("hr", "/api/attendance/check-out"))
    clock.set(datetime(2026, 10, 1, 18, 0))
    api.ok(api.post("ali", "/api/attendance/check-out"))
    api.ok(api.post("sara", "/api/attendance/check-out"))
    # Oct 2 (Fri): bilal on approved leave, others absent except ali
    leave = api.ok(api.post("bilal", "/api/leaves", {"leave_type": "casual", "start": "2026-10-02",
                                                     "end": "2026-10-02", "reason": "Errand"}), 201)
    api.ok(api.post("hr", f"/api/leaves/{leave['id']}/decision", {"approve": True}))
    clock.set(datetime(2026, 10, 2, 9, 5))
    api.ok(api.post("ali", "/api/attendance/check-in"), 201)
    clock.set(datetime(2026, 10, 5, 8, 0))  # Monday morning, nobody checked in yet
    stats = api.ok(api.get("hr", "/api/analytics/attendance?date_from=2026-10-01&date_to=2026-10-02"))
    # 4 employees x 2 working days = 8 records; on_leave 1; present 1 (Oct1 ali) ; late 1 ; half 1 ;
    # missing_checkout 1 (Oct2 ali) ; absent = 8 - 4 attended - 1 leave = 3
    assert (stats["expected"], stats["present"], stats["late"], stats["half_day"], stats["missing_checkout"],
            stats["on_leave"], stats["absent"]) == (8, 1, 1, 1, 1, 1, 3)
    assert stats["attendance_rate_pct"] == round((1 + 1 + 1 + 0.5) / (8 - 1) * 100, 1)
    dash = api.ok(api.get("hr", "/api/dashboard/hr?date_from=2026-10-01&date_to=2026-10-02"))
    assert dash["kpis"]["attendance_rate_pct"] == stats["attendance_rate_pct"]
    assert dash["top_late_comers"][0]["name"] == "Sara Ahmed"
    eng = api.ok(api.get("hr", f"/api/analytics/attendance?date_from=2026-10-01&date_to=2026-10-02"
                               f"&department_id={company['departments']['eng']['id']}"))
    assert eng["employees"] == 2 and eng["expected"] == 4
    # AI answers use the same services -> same numbers
    ans = api.ok(api.post("hr", "/api/ai/chat", {"message": "How many active employees are in Engineering?"}))
    assert "2 active employees in Engineering" in ans["answer"]


def test_admin_dashboard_attrition_and_payroll(api, company, clock):
    clock.set(datetime(2026, 10, 16, 12, 0))
    api.ok(api.post("hr", f"/api/employees/{company['people']['bilal']['id']}/exit", {"exit_date": "2026-10-16"}))
    dash = api.ok(api.get("admin", "/api/dashboard/admin"))
    assert dash["kpis"]["exits_ytd"] == 1 and dash["kpis"]["headcount"] == 4  # still employed on exit day
    # gross = 1.5 x basic: (80k + 100k + 120k + 60k) x 1.5 / 4 = 135,000
    assert dash["kpis"]["average_salary_gross"] == 135000.0
    eng = next(r for r in dash["average_salary"] if r["department"] == "Engineering")
    assert eng["average_gross"] == 165000.0
    assert {r["label"] for r in dash["headcount_by_department"]} == {"Engineering", "Sales", "Human Resources"}
    assert api.get("hr", "/api/dashboard/admin").status_code == 403
