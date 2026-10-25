"""Server-side role and data-scope checks, password rules, token revocation, audit immutability."""

from datetime import datetime

import pytest
from sqlalchemy import select

from hcm_lite.models import AuditLog, User


def test_temporary_password_must_be_changed_first(api, company):
    emp = api.ok(api.post("hr", "/api/employees", {
        "full_name": "New Joiner", "email": "new@acme.pk", "department_id": company["departments"]["eng"]["id"],
        "join_date": "2026-10-01", "basic_salary": 50000}), 201)
    data = api.login("new", "new@acme.pk", emp["temporary_password"])
    assert data["must_change_password"] is True
    blocked = api.get("new", "/api/dashboard/employee")
    assert blocked.status_code == 403 and blocked.json()["error"] == "password_change_required"
    weak = api.post("new", "/api/auth/change-password", {"current_password": emp["temporary_password"],
                                                          "new_password": "abcdefgh"})
    assert weak.status_code == 422


def test_employee_scope_is_own_rows_only(api, company):
    ali, sara = company["people"]["ali"], company["people"]["sara"]
    listing = api.ok(api.get("ali", "/api/employees"))
    assert listing["total"] == 1 and listing["items"][0]["id"] == ali["id"]
    assert api.get("ali", f"/api/employees/{sara['id']}").status_code == 403
    assert api.get("ali", f"/api/employees/{sara['id']}/360").status_code == 403
    assert api.get("ali", f"/api/leaves/balance?employee_id={sara['id']}").status_code == 403
    assert api.get("ali", f"/api/attendance/calendar?employee_id={sara['id']}").status_code == 403
    assert api.get("ali", f"/api/employees/{ali['id']}/salary").status_code == 403  # salary structure: HR/Admin
    assert api.get("ali", "/api/attendance/board").status_code == 403
    assert api.get("ali", "/api/dashboard/hr").status_code == 403


def test_role_boundaries(api, company):
    eng = company["departments"]["eng"]["id"]
    assert api.put("hr", "/api/settings", {"grace_minutes": 30}).status_code == 403  # HR cannot change settings
    assert api.post("hr", "/api/users/hr", {"full_name": "X HR", "email": "x@acme.pk", "department_id": eng,
                                            "join_date": "2026-01-01"}).status_code == 403
    assert api.post("ali", "/api/employees", {"full_name": "Hack", "email": "h@acme.pk", "department_id": eng,
                                              "join_date": "2026-01-01", "basic_salary": 1}).status_code == 403
    assert api.post("admin", "/api/employees", {"full_name": "Admin Made", "email": "a2@acme.pk", "department_id": eng,
                                                "join_date": "2026-01-01", "basic_salary": 1}).status_code == 403
    assert api.post("ali", "/api/announcements", {"title": "Hi", "body": "spam"}).status_code == 403
    assert api.get("admin", "/api/settings").status_code == 200
    assert api.get("hr", "/api/settings").status_code == 200  # HR may view
    assert api.post("admin", "/api/attendance/check-in").status_code == 403  # Admin has no employee profile


def test_employee_can_edit_only_phone_and_address(api, company):
    me = api.ok(api.patch("ali", "/api/me/profile", {"phone": "0300-1234567", "address": "Gulshan, Karachi"}))
    assert me["phone"] == "0300-1234567"
    assert api.patch("ali", f"/api/employees/{company['people']['ali']['id']}", {"full_name": "Boss"}).status_code == 403


def test_deactivate_and_reset_revoke_sessions(api, company):
    users = {u["email"]: u for u in api.ok(api.get("admin", "/api/users"))}
    bilal_id = users["bilal@acme.pk"]["id"]
    api.ok(api.post("admin", f"/api/users/{bilal_id}/deactivate"))
    assert api.get("bilal", "/api/dashboard/employee").status_code == 401
    assert api.post(None, "/api/auth/login", {"email": "bilal@acme.pk", "password": "Welcome@2026"}).status_code == 401
    api.ok(api.post("admin", f"/api/users/{bilal_id}/activate"))
    reset = api.ok(api.post("admin", f"/api/users/{bilal_id}/reset-password"))
    assert reset["must_change_password"] is True and reset["temporary_password"]
    assert api.post(None, "/api/auth/login", {"email": "bilal@acme.pk", "password": "Welcome@2026"}).status_code == 401


def test_passwords_are_bcrypt_hashed_and_audit_is_immutable(api, company):
    session = api.client.app.state.hcm.session_factory()
    try:
        user = session.scalar(select(User).where(User.email == "ali@acme.pk"))
        assert user.password_hash.startswith("$2") and "Welcome" not in user.password_hash
        entry = session.scalar(select(AuditLog).limit(1))
        entry.action = "tampered"
        with pytest.raises(PermissionError):
            session.flush()
        session.rollback()
        with pytest.raises(PermissionError):
            session.delete(session.scalar(select(AuditLog).limit(1)))
            session.flush()
    finally:
        session.rollback()
        session.close()


def test_audit_records_before_and_after(api, company, clock):
    clock.set(datetime(2026, 10, 2, 9, 0))
    api.ok(api.put("admin", "/api/settings", {"grace_minutes": 10}))
    entry = next(e for e in api.ok(api.get("admin", "/api/audit?entity=company_settings"))["items"])
    assert entry["before"]["grace_minutes"] == 15 and entry["after"]["grace_minutes"] == 10
    assert entry["actor_role"] == "admin"


def test_settings_apply_to_new_records_only(api, company, clock):
    clock.set(datetime(2026, 10, 5, 9, 20))
    assert api.ok(api.post("ali", "/api/attendance/check-in"), 201)["status"] == "late"
    api.ok(api.put("admin", "/api/settings", {"grace_minutes": 30}))
    clock.set(datetime(2026, 10, 5, 9, 25))
    assert api.ok(api.post("sara", "/api/attendance/check-in"), 201)["status"] == "present"
    clock.set(datetime(2026, 10, 5, 18, 0))
    assert api.ok(api.post("ali", "/api/attendance/check-out"))["status"] == "late"  # kept its snapshot
