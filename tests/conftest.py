"""Shared fixtures: in-memory DB, frozen clock, and an HTTP helper that acts as each role."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hcm_lite.clock import FrozenClock  # noqa: E402
from hcm_lite.config import Settings  # noqa: E402
from hcm_lite.main import create_app  # noqa: E402

ADMIN = {"email": "admin@acme.pk", "password": "Admin@12345"}
NEW_PASSWORD = "Welcome@2026"


@dataclass
class Api:
    client: TestClient
    tokens: dict[str, str] = field(default_factory=dict)

    def _headers(self, who: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"} if who else {}

    def get(self, who: str | None, url: str, **kw: Any):
        return self.client.get(url, headers=self._headers(who), **kw)

    def post(self, who: str | None, url: str, json: Any = None, **kw: Any):
        return self.client.post(url, json=json, headers=self._headers(who), **kw)

    def put(self, who: str | None, url: str, json: Any = None):
        return self.client.put(url, json=json, headers=self._headers(who))

    def delete(self, who: str | None, url: str):
        return self.client.delete(url, headers=self._headers(who))

    def patch(self, who: str | None, url: str, json: Any = None):
        return self.client.patch(url, json=json, headers=self._headers(who))

    def ok(self, resp, status: int = 200) -> Any:
        assert resp.status_code == status, f"{resp.status_code}: {resp.text}"
        return resp.json() if resp.content else None

    def login(self, who: str, email: str, password: str) -> dict[str, Any]:
        data = self.ok(self.post(None, "/api/auth/login", {"email": email, "password": password}))
        self.tokens[who] = data["access_token"]
        return data

    def onboard(self, who: str, email: str, temp_password: str) -> None:
        """Sign in with the temporary password and change it (required on first sign-in)."""
        self.login(who, email, temp_password)
        data = self.ok(self.post(who, "/api/auth/change-password",
                                 {"current_password": temp_password, "new_password": NEW_PASSWORD}))
        self.tokens[who] = data["access_token"]


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 10, 1, 8, 30))  # Thursday


TEST_DB = os.getenv("TEST_DATABASE_URL", "sqlite://")  # e.g. postgresql+psycopg://postgres@localhost:5433/postgres


@pytest.fixture
def settings() -> Settings:
    if TEST_DB != "sqlite://":  # fresh schema per test on a real server
        from sqlalchemy import text

        from hcm_lite import models  # noqa: F401
        from hcm_lite.db import Base, make_engine

        engine = make_engine(TEST_DB)
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()
    return Settings(database_url=TEST_DB, jwt_secret="test-secret-key-that-is-long-enough-123", bcrypt_rounds=4, ai_api_key=None,
                    ai_rate_limit_per_hour=20)


@pytest.fixture
def api(settings: Settings, clock: FrozenClock) -> Api:
    app = create_app(settings, clock=clock)
    return Api(TestClient(app))


@pytest.fixture
def company(api: Api) -> dict[str, Any]:
    """Fresh install -> Admin setup -> departments -> HR login -> 3 employees (all onboarded)."""
    api.ok(api.post(None, "/api/setup", {"company_name": "Acme Pvt Ltd", "admin_email": ADMIN["email"],
                                         "admin_password": ADMIN["password"]}), 201)
    api.login("admin", ADMIN["email"], ADMIN["password"])
    # Rule-focused tests use the web-button method; camera attendance has its own tests (test_camera_attendance.py)
    api.ok(api.put("admin", "/api/settings", {"office_lat": 24.8607, "office_lng": 67.0011, "office_radius_m": 150,
                                              "attendance_mode": "web"}))
    eng = api.ok(api.post("admin", "/api/departments", {"name": "Engineering"}), 201)
    sales = api.ok(api.post("admin", "/api/departments", {"name": "Sales"}), 201)
    hrd = api.ok(api.post("admin", "/api/departments", {"name": "Human Resources"}), 201)
    dev = api.ok(api.post("admin", "/api/designations", {"name": "Developer"}), 201)
    hr = api.ok(api.post("admin", "/api/users/hr", {
        "full_name": "Hina HR", "email": "hina@acme.pk", "department_id": hrd["id"], "join_date": "2025-01-01",
        "basic_salary": 80000}), 201)
    api.onboard("hr", "hina@acme.pk", hr["temporary_password"])
    people = {}
    for key, name, email, dept, basic, join in [
        ("ali", "Ali Khan", "ali@acme.pk", eng["id"], 100000, "2025-03-01"),
        ("sara", "Sara Ahmed", "sara@acme.pk", eng["id"], 120000, "2024-06-15"),
        ("bilal", "Bilal Raza", "bilal@acme.pk", sales["id"], 60000, "2025-02-01"),
    ]:
        emp = api.ok(api.post("hr", "/api/employees", {
            "full_name": name, "email": email, "department_id": dept, "designation_id": dev["id"],
            "join_date": join, "basic_salary": basic, "gender": "male" if key != "sara" else "female"}), 201)
        api.onboard(key, email, emp["temporary_password"])
        people[key] = emp
    return {"departments": {"eng": eng, "sales": sales, "hr": hrd}, "people": people, "hr": hr}
