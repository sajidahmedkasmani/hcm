"""Demo harness — NOT part of the app.

Plays the judges' roles (Admin, HR, employees) through the real HTTP API with a controllable
clock, so a fresh install accumulates a realistic month of activity in a few seconds. Nothing is
inserted into the database directly: every record comes from an API call, exactly as in the app.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi.testclient import TestClient

from hcm_lite.clock import FrozenClock

PASSWORD = "Welcome@2026"


@dataclass
class ApiClient:
    client: TestClient
    tokens: dict[str, str] = field(default_factory=dict)

    def call(self, who: str | None, method: str, url: str, json: Any = None, expect: int | None = None, **kw: Any):
        headers = {"Authorization": f"Bearer {self.tokens[who]}"} if who else {}
        resp = self.client.request(method, url, json=json, headers=headers, **kw)
        if expect is not None and resp.status_code != expect:
            raise AssertionError(f"{method} {url} as {who}: {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.content and resp.headers.get("content-type", "").startswith("application/json") else resp

    def raw(self, who: str | None, method: str, url: str, json: Any = None, **kw: Any):
        """Return the raw HTTP response (to inspect status codes such as 403 / 409 / 422)."""
        headers = {"Authorization": f"Bearer {self.tokens[who]}"} if who else {}
        return self.client.request(method, url, json=json, headers=headers, **kw)

    def get(self, who, url, **kw):
        return self.call(who, "GET", url, expect=kw.pop("expect", 200), **kw)

    def post(self, who, url, json=None, **kw):
        return self.call(who, "POST", url, json=json, **kw)

    def login(self, who: str, email: str, password: str) -> dict[str, Any]:
        data = self.post(None, "/api/auth/login", {"email": email, "password": password}, expect=200)
        self.tokens[who] = data["access_token"]
        return data

    def onboard(self, who: str, email: str, temp_password: str) -> None:
        self.login(who, email, temp_password)
        data = self.post(who, "/api/auth/change-password",
                         {"current_password": temp_password, "new_password": PASSWORD}, expect=200)
        self.tokens[who] = data["access_token"]


# name, email key, department, designation, basic, join date, gender, behaviour profile
STAFF = [
    ("Ali Raza", "ali", "Engineering", "Senior Engineer", 220000, "2023-04-03", "male", "punctual"),
    ("Ayesha Khan", "ayesha", "Engineering", "Software Engineer", 150000, "2024-08-12", "female", "punctual"),
    ("Hamza Sheikh", "hamza", "Engineering", "Software Engineer", 140000, "2025-01-06", "male", "chronic_late"),
    ("Zainab Malik", "zainab", "Engineering", "Team Lead", 260000, "2022-02-14", "female", "average"),
    ("Bilal Ahmed", "bilal", "Sales", "Sales Executive", 90000, "2025-05-19", "male", "absentee"),
    ("Sana Iqbal", "sana", "Sales", "Sales Executive", 95000, "2024-11-04", "female", "average"),
    ("Usman Tariq", "usman", "Sales", "Team Lead", 170000, "2023-09-18", "male", "chronic_late"),
    ("Fatima Noor", "fatima", "Finance", "Accountant", 120000, "2024-03-11", "female", "punctual"),
    ("Omar Farooq", "omar", "Finance", "Accountant", 110000, "2025-07-01", "male", "half_dayer"),
    ("Mahnoor Javed", "mahnoor", "Operations", "Ops Associate", 70000, "2025-10-01", "female", "average"),
    ("Danish Ali", "danish", "Operations", "Ops Associate", 65000, "2026-10-12", "male", "punctual"),  # joiner
    ("Hira Aslam", "hira", "Operations", "Ops Associate", 68000, "2024-01-15", "female", "average"),  # leaver
]
PROFILES = {  # (absence prob, mean minutes after 09:00, std, half-day prob, forgot-checkout prob)
    "punctual": (0.01, -8, 6, 0.00, 0.01),
    "average": (0.05, 6, 9, 0.03, 0.03),
    "chronic_late": (0.03, 24, 12, 0.02, 0.04),
    "absentee": (0.24, 10, 10, 0.05, 0.05),
    "half_dayer": (0.03, 4, 8, 0.25, 0.02),
}


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm))


def working_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def setup_company(api: ApiClient, clock: FrozenClock, *, admin_email: str = "admin@saylanitech.pk",
                  admin_password: str = "Admin@2026") -> dict[str, Any]:
    """Demo script steps 1-2: Admin first-run, settings, org structure, HR login; HR registers staff."""
    clock.set(at(date(2026, 9, 30), 10))
    api.post(None, "/api/setup", {"company_name": "Saylani Tech (Pvt) Ltd", "admin_name": "Sadia Admin",
                                  "admin_email": admin_email, "admin_password": admin_password}, expect=201)
    api.login("admin", admin_email, admin_password)
    api.call("admin", "PUT", "/api/settings", {"office_start": "09:00", "grace_minutes": 15, "half_day_hours": 5,
                                               "weekend_days": [5, 6], "office_lat": 24.8932, "office_lng": 67.0726,
                                               "office_radius_m": 150,
                                               # the month-long simulation has no cameras, so it uses the web method
                                               "attendance_mode": "web"}, expect=200)
    depts = {n: api.post("admin", "/api/departments", {"name": n}, expect=201)["id"]
             for n in ["Engineering", "Sales", "Finance", "Operations", "Human Resources"]}
    desigs = {n: api.post("admin", "/api/designations", {"name": n}, expect=201)["id"]
              for n in ["Software Engineer", "Senior Engineer", "Team Lead", "Sales Executive", "Accountant",
                        "Ops Associate", "HR Officer"]}
    for d, name in [("2026-11-09", "Iqbal Day"), ("2026-12-25", "Quaid-e-Azam Day")]:
        api.post("admin", "/api/holidays", {"date": d, "name": name}, expect=201)
    hr = api.post("admin", "/api/users/hr", {"full_name": "Hina Siddiqui", "email": "hina@saylanitech.pk",
                                             "department_id": depts["Human Resources"], "join_date": "2023-01-02",
                                             "designation_id": desigs["HR Officer"], "basic_salary": 130000,
                                             "gender": "female"}, expect=201)
    api.onboard("hr", "hina@saylanitech.pk", hr["temporary_password"])
    api.call("hr", "PUT", f"/api/employees/{hr['id']}/salary", {"basic": 130000, "effective_from": "2023-01-02"})
    people: dict[str, dict[str, Any]] = {"hr": {**hr, "profile": "punctual"}}
    for name, key, dept, desig, basic, join, gender, profile in STAFF:
        emp = api.post("hr", "/api/employees", {
            "full_name": name, "email": f"{key}@saylanitech.pk", "department_id": depts[dept],
            "designation_id": desigs[desig], "join_date": join, "basic_salary": basic, "gender": gender,
            "status": "probation" if join >= "2025-07-01" else "active"}, expect=201)
        api.onboard(key, emp["email"], emp["temporary_password"])
        people[key] = {**emp, "profile": profile}
    api.post("hr", "/api/announcements", {"title": "Welcome to HCM Lite",
                                          "body": "Check in from Home each morning. Leave requests now go through the app."},
             expect=201)
    return {"departments": depts, "designations": desigs, "people": people}


def simulate_days(api: ApiClient, clock: FrozenClock, people: dict[str, dict[str, Any]], start: date, end: date,
                  *, seed: int = 26, stop_at: time | None = None) -> dict[str, int]:
    """Daily check-in/out for every employee according to a behaviour profile.

    `stop_at` freezes the last day mid-way (e.g. 10:30) so 'today' has a live, partial board.
    """
    rng = random.Random(seed)
    stats = {"check_ins": 0, "absences": 0, "half_days": 0, "missing_checkout": 0}
    days = working_days(start, end)
    for d in days:
        events = []
        for key, p in people.items():
            if d < date.fromisoformat(p["join_date"]) or (p.get("exit_date") and d > date.fromisoformat(p["exit_date"])):
                continue
            absent_p, mean, std, half_p, forget_p = PROFILES[p["profile"]]
            if rng.random() < absent_p:
                stats["absences"] += 1
                continue
            cin = at(d, 9) + timedelta(minutes=max(-35, int(rng.gauss(mean, std))))
            half = rng.random() < half_p
            cout = cin + timedelta(hours=rng.uniform(3, 4.6) if half else rng.uniform(8.2, 9.3))
            events.append((cin, key, "in"))
            if rng.random() >= forget_p:
                events.append((cout, key, "out"))
                stats["half_days"] += int(half)
            else:
                stats["missing_checkout"] += 1
        for when, key, kind in sorted(events):
            if stop_at is not None and d == days[-1] and when.time() > stop_at:
                continue
            clock.set(when)
            api.post(key, f"/api/attendance/check-{kind}")
            stats["check_ins"] += int(kind == "in")
    return stats


def _approved_leave_days(api: ApiClient) -> dict[int, set[date]]:
    out: dict[int, set[date]] = {}
    for r in api.get("hr", "/api/leaves?status=approved")["items"]:
        out.setdefault(r["employee_id"], set()).update(
            working_days(date.fromisoformat(r["start"]), date.fromisoformat(r["end"])))
    return out


def simulate_range(api: ApiClient, clock: FrozenClock, people: dict[str, dict[str, Any]], start: date, end: date,
                    seed: int, stop_at: time | None = None) -> dict[str, int]:
    totals = {"check_ins": 0, "absences": 0, "half_days": 0, "missing_checkout": 0}
    for d in working_days(start, end):
        on_leave = _approved_leave_days(api)
        todays = {k: v for k, v in people.items() if d not in on_leave.get(v["id"], set())}
        s = simulate_days(api, clock, todays, d, d, seed=seed + d.toordinal(), stop_at=stop_at if d == end else None)
        for k in totals:
            totals[k] += s[k]
        if d == date(2026, 10, 16) and "hira" in people:  # HR records Hira's exit after her last day
            clock.set(at(d, 18, 30))
            exit_ = api.post("hr", f"/api/employees/{people['hira']['id']}/exit", {"exit_date": "2026-10-16"}, expect=200)
            people["hira"]["exit_date"] = exit_["exit_date"]
    return totals


def simulate_october(api: ApiClient, clock: FrozenClock, seed: int = 26) -> dict[str, Any]:
    """Setup + 1-23 Oct activity (23 Oct frozen at 10:30) + leave and correction workflows."""
    org = setup_company(api, clock)
    people = org["people"]
    clock.set(at(date(2026, 10, 1), 11))
    plans = [
        ("sana", {"leave_type": "annual", "start": "2026-10-07", "end": "2026-10-09", "reason": "Family trip to Hunza"}, True),
        ("ayesha", {"leave_type": "sick", "start": "2026-10-14", "end": "2026-10-15", "reason": "Fever"}, True),
        ("bilal", {"leave_type": "unpaid", "start": "2026-10-19", "end": "2026-10-20", "reason": "Personal matter"}, True),
        ("zainab", {"leave_type": "casual", "start": "2026-10-23", "end": "2026-10-23", "reason": "Nephew's aqiqa"}, True),
        ("usman", {"leave_type": "casual", "start": "2026-10-05", "end": "2026-10-05", "reason": "Bank work"}, False),
    ]
    for who, body, approve in plans:
        req = api.post(who, "/api/leaves", body, expect=201)
        api.post("hr", f"/api/leaves/{req['id']}/decision",
                 {"approve": approve, "comment": None if approve else "Month-end closing, please pick another day"},
                 expect=200)
    stats = simulate_range(api, clock, people, date(2026, 10, 1), date(2026, 10, 23), seed, stop_at=time(10, 30))

    # A forgotten check-in fixed through a correction request that HR approves
    clock.set(at(date(2026, 10, 22), 16))
    board = api.get("hr", "/api/attendance/board?day=2026-10-21")
    missing = next((r for r in board["rows"] if r["status"] == "absent"), None)
    if missing:
        key = next(k for k, p in people.items() if p["id"] == missing["employee_id"])
        corr = api.post(key, "/api/attendance/corrections",
                        {"date": "2026-10-21", "check_in": "09:05", "check_out": "17:40",
                         "reason": "Was at the client site in DHA, phone battery died"}, expect=201)
        api.post("hr", f"/api/attendance/corrections/{corr['id']}/decision",
                 {"approve": True, "comment": "Verified with team lead"}, expect=200)
    clock.set(at(date(2026, 10, 23), 10, 30))
    return {**org, "stats": stats}


def finish_month(api: ApiClient, clock: FrozenClock, people: dict[str, dict[str, Any]], seed: int = 99) -> dict[str, int]:
    """Finish 23 Oct (evening check-outs), simulate 26-30 Oct, then stop the clock on Sat 31 Oct."""
    clock.set(at(date(2026, 10, 23), 18, 5))
    for key, p in people.items():
        if not p.get("exit_date"):
            api.post(key, "/api/attendance/check-out")
    stats = simulate_range(api, clock, people, date(2026, 10, 26), date(2026, 10, 30), seed)
    clock.set(at(date(2026, 10, 31), 11))
    return stats
