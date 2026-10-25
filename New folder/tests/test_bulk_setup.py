"""Bulk setup: many departments/designations in one request, and employee import from Excel."""

from __future__ import annotations

import io
from datetime import datetime

from openpyxl import Workbook

DEPARTMENTS = ["Engineering", "Sales", "Finance", "Operations", "Marketing", "Customer Support", "Legal",
               "Procurement", "Quality Assurance", "Administration"]  # 10 (Engineering already exists in fixture)
DESIGNATIONS = [f"Level {i} Specialist" for i in range(1, 50)]       # 49


def test_bulk_departments_skips_existing_repeats_blanks_and_header(api, company):
    pasted = ["Department", *DEPARTMENTS, "  sales ", "", "Legal", "Data   Science"]
    report = api.ok(api.post("admin", "/api/departments/bulk", {"names": pasted}))
    created = [d["name"] for d in report["created"]]
    assert created == [d for d in DEPARTMENTS if d not in ("Engineering", "Sales")] + ["Data Science"]
    reasons = {(s["name"], s["reason"]) for s in report["skipped"]}
    assert ("Engineering", "already exists") in reasons and ("Sales", "already exists") in reasons
    assert ("Legal", "repeated in the list") in reasons
    names = {d["name"] for d in api.ok(api.get("admin", "/api/departments"))}
    assert len(names) == 3 + 9  # fixture's 3 + 9 new (Engineering & Sales existed; Human Resources from fixture)
    again = api.ok(api.post("admin", "/api/departments/bulk", {"names": DEPARTMENTS}))
    assert again["created"] == [] and len(again["skipped"]) == 10  # pasting twice is safe


def test_bulk_designations_49_in_one_request_and_audited(api, company):
    report = api.ok(api.post("admin", "/api/designations/bulk", {"names": DESIGNATIONS}))
    assert len(report["created"]) == 49 and report["skipped"] == []
    audit = api.ok(api.get("admin", "/api/audit?entity=designation&size=100"))
    assert audit["total"] >= 49


def test_bulk_is_admin_only_and_validated(api, company):
    assert api.post("hr", "/api/departments/bulk", {"names": ["X Dept"]}).status_code == 403
    assert api.post("ali", "/api/designations/bulk", {"names": ["Boss"]}).status_code == 403
    assert api.post("admin", "/api/departments/bulk", {"names": []}).status_code == 422
    assert api.post("admin", "/api/departments/bulk", {"names": ["", "  "]}).status_code == 422
    assert api.post("admin", "/api/departments/bulk", {"names": [f"D{i}" for i in range(501)]}).status_code == 422
    long = api.ok(api.post("admin", "/api/departments/bulk", {"names": ["x" * 121, "Short Name"]}))
    assert long["skipped"][0]["reason"] == "longer than 120 characters" and long["created"][0]["name"] == "Short Name"


def _xlsx(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(api, who, content: bytes, filename: str, dry_run: bool):
    files = {"file": (filename, content, "application/octet-stream")}
    return api.client.post(f"/api/employees/import?dry_run={str(dry_run).lower()}", files=files,
                           headers={"Authorization": f"Bearer {api.tokens[who]}"})


def test_employee_import_from_excel_with_real_date_and_number_cells(api, company):
    header = ["full_name", "email", "department", "designation", "join_date", "basic_salary", "phone"]
    good = _xlsx([header,
                  ["Zara Malik", "zara@acme.pk", "engineering", "Developer", datetime(2026, 11, 2), 95000, 3001234567],
                  ["Usman Ali", "usman@acme.pk", "Sales", None, "2026-11-02", 70000.0, None],
                  [None, None, None, None, None, None, None]])          # blank row is ignored
    dry = api.ok(_upload(api, "hr", good, "staff.xlsx", dry_run=True))
    assert dry["valid_rows"] == 2 and dry["errors"] == []
    done = api.ok(_upload(api, "hr", good, "staff.xlsx", dry_run=False))
    assert [c["email"] for c in done["created"]] == ["zara@acme.pk", "usman@acme.pk"]
    zara = api.ok(api.get("hr", "/api/employees?q=zara"))["items"][0]
    assert zara["join_date"] == "2026-11-02" and zara["phone"] == "3001234567"
    assert zara["department"]["name"] == "Engineering"  # department matching is case-insensitive


def test_excel_import_reports_problems_by_row_and_field(api, company):
    bad = _xlsx([["full_name", "email", "department", "join_date", "basic_salary"],
                 ["No Dept", "nodept@acme.pk", "Astrology", datetime(2026, 11, 2), 50000],
                 ["Bad Pay", "badpay@acme.pk", "Sales", datetime(2026, 11, 2), -1]])
    report = api.ok(_upload(api, "hr", bad, "bad.xlsx", dry_run=True))
    errors = {(e["row"], e["field"]) for e in report["errors"]}
    assert errors == {(2, "department"), (3, "basic_salary")}
    assert _upload(api, "hr", bad, "bad.xlsx", dry_run=False).status_code == 422  # all-or-nothing


def test_unreadable_files_get_clear_messages(api, company):
    assert "valid .xlsx" in _upload(api, "hr", b"not a zip", "broken.xlsx", True).json()["message"]
    assert ".xls files aren't supported" in _upload(api, "hr", b"\xd0\xcf", "old.xls", True).json()["message"]
    assert "UTF-8" in _upload(api, "hr", "naïve".encode("latin-1"), "latin.csv", True).json()["message"]


def test_import_template(api, company):
    resp = api.get("hr", "/api/employees/import-template.csv")
    assert resp.status_code == 200 and resp.text.splitlines()[0].startswith("employee_code,full_name,email,department")
    assert api.get("ali", "/api/employees/import-template.csv").status_code == 403
