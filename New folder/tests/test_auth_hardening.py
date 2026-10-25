"""Findings 1 & 2: server-side logout, generic sign-in errors, login throttling, session revocation."""

from datetime import timedelta

import pytest

from tests.conftest import ADMIN, NEW_PASSWORD


def _login_raw(api, email, password):
    return api.post(None, "/api/auth/login", {"email": email, "password": password})


def test_logout_revokes_this_token_only(api, company):
    first = api.tokens["ali"]
    second = api.login("ali2", "ali@acme.pk", NEW_PASSWORD)["access_token"]
    assert api.post("ali", "/api/auth/logout").status_code == 204
    reused = api.client.get("/api/auth/me", headers={"Authorization": f"Bearer {first}"})
    assert reused.status_code == 401 and reused.json()["error"] == "token_revoked"
    assert api.client.get("/api/auth/me", headers={"Authorization": f"Bearer {second}"}).status_code == 200
    # a revoked token can't be used to "log out" again either
    assert api.client.post("/api/auth/logout", headers={"Authorization": f"Bearer {first}"}).status_code == 401


def test_logout_everywhere_revokes_all_sessions(api, company):
    other = api.login("ali2", "ali@acme.pk", NEW_PASSWORD)["access_token"]
    assert api.post("ali", "/api/auth/logout?everywhere=true").status_code == 204
    for token in (api.tokens["ali"], other):
        assert api.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert _login_raw(api, "ali@acme.pk", NEW_PASSWORD).status_code == 200  # can sign in again


def test_errors_are_generic_for_unknown_wrong_and_inactive(api, company):
    unknown = _login_raw(api, "nobody@acme.pk", "Whatever@123")
    wrong = _login_raw(api, "sara@acme.pk", "Wrong@12345")
    users = {u["email"]: u for u in api.ok(api.get("admin", "/api/users"))}
    api.ok(api.post("admin", f"/api/users/{users['bilal@acme.pk']['id']}/deactivate"))
    inactive_right_password = _login_raw(api, "bilal@acme.pk", NEW_PASSWORD)
    bodies = [r.json() for r in (unknown, wrong, inactive_right_password)]
    assert {r.status_code for r in (unknown, wrong, inactive_right_password)} == {401}
    assert bodies[0] == bodies[1] == bodies[2] == {"error": "invalid_credentials", "message": "Invalid email or password"}


def test_repeated_failures_lock_the_account_then_expire(api, company, clock):
    for _ in range(5):
        assert _login_raw(api, "sara@acme.pk", "Wrong@12345").status_code == 401
    locked = _login_raw(api, "sara@acme.pk", NEW_PASSWORD)  # even the right password is refused while locked
    assert locked.status_code == 429 and locked.json()["error"] == "login_throttled"
    assert int(locked.headers["Retry-After"]) == 15 * 60 + 1  # window is inclusive: unlocked one second later
    assert _login_raw(api, "ali@acme.pk", NEW_PASSWORD).status_code == 200  # other accounts unaffected
    clock.advance(minutes=15, seconds=1)
    assert _login_raw(api, "sara@acme.pk", NEW_PASSWORD).status_code == 200
    audit = api.ok(api.get("admin", "/api/audit?action=lockout"))["items"]
    assert audit and audit[0]["after"]["email"] == "sara@acme.pk"


def test_successful_sign_in_resets_the_email_counter(api, company):
    for _ in range(4):
        _login_raw(api, "sara@acme.pk", "Wrong@12345")
    assert _login_raw(api, "sara@acme.pk", NEW_PASSWORD).status_code == 200
    for _ in range(4):
        assert _login_raw(api, "sara@acme.pk", "Wrong@12345").status_code == 401
    assert _login_raw(api, "sara@acme.pk", NEW_PASSWORD).status_code == 200


def test_per_ip_limit_stops_password_spraying(api, company):
    """20 failures from one client across many emails -> that client is throttled."""
    for i in range(20):
        _login_raw(api, f"guess{i}@acme.pk", "Spray@12345")
    sprayed = _login_raw(api, "ali@acme.pk", NEW_PASSWORD)
    assert sprayed.status_code == 429


def test_temporary_password_and_reset_flows(api, company):
    users = {u["email"]: u for u in api.ok(api.get("admin", "/api/users"))}
    reset = api.ok(api.post("admin", f"/api/users/{users['sara@acme.pk']['id']}/reset-password"))
    assert api.get("sara", "/api/dashboard/employee").status_code == 401  # old session revoked by reset
    data = api.login("sara", "sara@acme.pk", reset["temporary_password"])
    assert data["must_change_password"] is True
    assert api.get("sara", "/api/dashboard/employee").json()["error"] == "password_change_required"
    changed = api.ok(api.post("sara", "/api/auth/change-password",
                              {"current_password": reset["temporary_password"], "new_password": "Brand@New2026"}))
    stale = api.tokens["sara"]
    assert api.client.get("/api/auth/me", headers={"Authorization": f"Bearer {stale}"}).status_code == 401
    api.tokens["sara"] = changed["access_token"]
    assert api.get("sara", "/api/dashboard/employee").status_code == 200


def test_admin_login_still_works_after_hardening(api, company):
    assert _login_raw(api, ADMIN["email"], ADMIN["password"]).status_code == 200


@pytest.mark.parametrize("bad", ["", "x" * 129])
def test_login_input_validation(api, company, bad):
    assert _login_raw(api, "ali@acme.pk", bad).status_code == 422


def test_tokens_expire(api, company, settings):
    import jwt

    claims = jwt.decode(api.tokens["ali"], settings.jwt_secret, algorithms=["HS256"])
    assert claims["exp"] - claims["iat"] == int(timedelta(minutes=settings.jwt_expires_minutes).total_seconds())
    assert claims["jti"] and claims["tv"] >= 1
