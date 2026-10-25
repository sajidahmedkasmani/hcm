# HCM Lite — AI-assisted Human Capital Management

Saylani Hackathon, 24 October 2026. A web app for Admin, HR and Employees: setup, employees, web check-in/out,
leave with approval, monthly payroll with PDF payslips, dashboards, and an AI assistant that answers only from
the app's own data and the signed-in user's access.

**Stack:** Python 3.11+, FastAPI, SQLAlchemy 2 (SQLite or PostgreSQL), PyJWT + bcrypt, ReportLab (payslip PDF),
vanilla-JS single-page UI served by the API (no build step), any OpenAI-compatible LLM with tool calling.

## Run it (about 5 minutes)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # set JWT_SECRET; AI_API_KEY optional
uvicorn hcm_lite.main:create_app --factory --reload
```

Open http://127.0.0.1:8000. A fresh install shows **Set up your company**: enter the company name and the Admin
email/password. (Alternatively set `ADMIN_EMAIL` / `ADMIN_PASSWORD` / `COMPANY_NAME` in `.env` and the Admin is
created on startup.) API docs: http://127.0.0.1:8000/docs

**Set the AI key:** put `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL` in `.env` (Groq and Gemini examples are in
`.env.example`). The key is read only by the backend. Without a key, the assistant runs on the built-in offline
engine, which uses the same tools and permission checks.

**Tests:** `pip install -r requirements-dev.txt && pytest` — 124 tests, passing on SQLite and on PostgreSQL 16
(`TEST_DATABASE_URL=postgresql+psycopg://...`). `pytest -m live` runs the AI red-team matrix against the hosted
model in `AI_API_KEY` (skipped without a key).

**Database migrations (Alembic):** the schema is versioned in `hcm_lite/migrations`. On startup the app runs
`upgrade head` (set `AUTO_MIGRATE=false` to run `alembic upgrade head` yourself as a pre-deploy step; the app then
refuses to start on an out-of-date schema). A database created by v1.0 with `create_all` is adopted automatically
(stamped at the baseline `0001`, then upgraded). After changing `models.py`:
`alembic revision --autogenerate -m "..."` — a test fails if models and migrations drift apart.

**Deploy (free):** Render (`render.yaml` included) or Railway/Fly with the `Procfile`. Use a free hosted PostgreSQL
(Neon, Supabase, Render) for `DATABASE_URL` — SQLite on free hosts is wiped on restart. Keep secrets in the host's
environment settings; commit `.env.example` only.

## Demo script (5 minutes)

1. Fresh install → Admin sets up the company → **Organization**: office hours, grace time, location + radius,
   departments and designations (type one, or **Add many at once**: paste a column from Excel), leave types →
   **Logins**: create an HR login (shows the temporary password once).
2. HR signs in (forced password change) → **Employees** → register 2–3 employees (each gets a temporary password),
   or **Import from Excel/CSV** for a whole list (dry run first, then all-or-nothing import).
3. An employee signs in, changes the password, presses **Check in** on Home.
4. Employee → **Leave** → types *"Need next Monday and Tuesday off for my sister's wedding"* → **Fill the form**
   → confirms → HR approves in the **Approval inbox** with a comment.
5. HR → **Dashboard** → **Summarize**, and asks the assistant *"Who is on leave today?"* → **Payroll** → create the
   month's draft → Admin → **Payroll** → **Finalize** (month is locked).
6. Employee → **Payslips** → download the PDF; asks *"What is {colleague}'s salary?"* → politely refused.
   Admin → **Audit log**.

## Architecture

```
Browser (SPA, role menus)  --REST/JSON + JWT-->  FastAPI routes (thin)
                                                    |
                                     services/  (all business rules + RBAC + data scope)
                                      |   org · auth · employees · attendance · leave · payroll
                                      |   analytics (SQL aggregation) · insights · comms · audit
                                      v
                                  SQLAlchemy  -->  SQLite / PostgreSQL
AI (M8):  /api/ai/*  ->  AIService  ->  ReAct tool loop  ->  ToolRegistry  ->  the SAME services
                                     \-> LLM provider (OpenAI-compatible) with offline fallback
```

* **Routes → services → data.** Routes validate input (Pydantic) and commit once per request; services hold every
  rule from the brief; guards (`require_roles`, `assert_can_view_employee`) run on the server for every request.
* **Clock injection.** The server stamps all times from an injectable clock (`SystemClock` in production,
  `FrozenClock` in tests and the notebook simulation).
* **Settings apply to new records only.** Each attendance record keeps a snapshot of the rules used.

### AI assistant architecture (M8)

| Feature | Pattern | Guardrail |
|---|---|---|
| Ask the assistant | Single-agent **ReAct tool-calling loop** (reason → call a scoped tool → observe → answer), max 4 steps | Tools are filtered by role *and* every tool calls services that re-check scope; a forbidden tool result makes the model refuse |
| Dashboard summary | Grounded summarization over server-recomputed KPI JSON | **Numeric hallucination guard:** every number in the bullets must exist in the KPI payload → retry once → deterministic fallback |
| Smart leave | Structured extraction → Pydantic validation → retry once → normal form | Never saves; preview shows working days, balance and conflicts |
| Policy Q&A | Rules rendered from live settings into the system prompt; offline mode uses TF-IDF retrieval over the same rules | Answers "I don't know" when not covered |

Also: 15 s budget per request, 20 requests/user/hour across chat, summary and smart leave (counted from `ai_logs`,
429 with `Retry-After`), every call logged in `ai_logs`, answers labelled **AI-generated**, Roman Urdu understood.
A tool error or a malformed provider response never surfaces as a 500.

**Red-team matrix** (`scripts/ai_redteam.py`, `tests/test_ai_adversarial.py`): 22 cases — direct and indirect salary
questions, role-claim and "SYSTEM OVERRIDE" injection, aggregates ("average salary in Engineering", "highest paid"),
enumeration, forged chat history, Roman Urdu — judged by a leak oracle (no colleague pay figure in any number format,
no colleague name the user didn't type, no pay figures at all when the question is about someone else). Plus a
**compromised-model test**: a scripted model calls every tool with colleague-targeted arguments and echoes all
results — nothing leaks, because scope is enforced in the services, not the prompt. Run it against your provider:
`python scripts/ai_redteam.py --live`.

**Data note:** when HR/Admin ask the hosted model about payroll, the tool results (which can include salaries) are
sent to that LLM provider. Choose a provider whose data terms you accept, or leave `AI_API_KEY` empty to keep
everything on the server (offline engine). Tools: `get_headcount`, `get_attendance_summary`,
`get_late_comers`, `get_leave_summary`, `get_my_leave_balance`, `get_payroll_summary`, `search_employees`.

## Bulk setup

* **Departments and designations:** Organization → *Add many at once* → paste one name per line (a column copied
  from Excel or Google Sheets) or load a .csv/.txt file → **Add all**. Names that already exist (any capitalisation),
  repeats, blank lines and a header row are skipped, so pasting the same list twice is safe. API:
  `POST /api/departments/bulk` and `/api/designations/bulk` with `{"names": [...]}` (Admin, up to 500 per call).
* **Employees (HR):** Employees → *Import from Excel/CSV* → upload an **.xlsx** (first sheet; real Excel dates are fine)
  or CSV → **Check file** lists every problem by row, field and reason → **Import** creates everyone (with temporary
  passwords) only when there are no errors. *Download template* gives the column headers; the dialog lists the valid
  department and designation names. Departments must exist first — HR can't create them (brief: Admin manages them).

## Security

* bcrypt password hashes; HS256 JWTs with a unique `jti`, the user's `token_version`, and expiry.
* **Server-side logout:** `POST /api/auth/logout` denylists that token's `jti` until it would have expired;
  `?everywhere=true` bumps `token_version`, revoking every session. Password change, reset and deactivation also
  revoke sessions.
* **Sign-in throttling:** 5 failures per email or 20 per client IP within 15 minutes → `429` with `Retry-After`
  (even the right password is refused while locked); attempts are stored in the database, so limits hold across
  workers and restarts; lockouts are written to the audit log.
* **Generic errors:** unknown email, wrong password and deactivated account return the same 401 body, and an unknown
  email still runs a bcrypt check so response time doesn't reveal which accounts exist.
* Temporary passwords must be changed before any other call; role and data-scope checks in every service.
* Trade-off: per-email throttling lets someone lock a known account for up to 15 minutes; per-IP limits need the
  real client IP, so the start commands use `--proxy-headers`.

## Business rules implemented

* **Attendance:** server time; one record per employee per day (2nd check-in → 409); late after start + grace
  (09:15, minute precision); half day under 5 h; missing check-out derived for past days; absent = working day with
  no record and no approved leave; check-in blocked on weekends/holidays and on approved leave; corrections need HR.
  Absences count only from the later of join date and registration (no login = no absence).
* **Leave:** working days only; quotas annual 14, sick 8, casual 10, unpaid unlimited; overlap or over-balance → 422;
  pending requests reserve balance; HR can't approve their own requests.
* **Payroll:** gross = 1.5 × basic (HRA 40 %, medical 10 %); per day = gross ÷ working days; deductions = absent +
  unpaid + 0.5 × half days (+ optional late penalty: 3 lates = 0.5 day); joiners/leavers prorated; tax slab on
  gross × 12, never more than pay left after deductions; HR drafts, only Admin finalizes, finalized months are locked.
  Verified by 12 hand-computed known-answer cases and a full November scenario (holiday, paid vs unpaid leave,
  absence + half days, joiner, leaver) reconciled from payslips to run totals, dashboard and employee view.
  Simplification: a payslip uses the salary structure in force on the last day of the month (no mid-month split).
* **Metrics:** exactly the Section 7 formulas, computed with SQL `GROUP BY` + working-day calendar, filterable by
  date range and department. The assistant calls the same functions, so its numbers match the dashboards.

## What is done / not done


**Done:** all 25 functionalities in the checklist; three role dashboards; Excel/CSV employee import with dry-run report,
template and export; bulk add for departments and designations;
in-app notifications; append-only audit log with before/after JSON; responsive UI (375 px+), dark mode.
Bonus: camera check-in (server-side Haversine geofence, one-face and GPS-accuracy rules; browser face detection
with face-api.js), explainable attrition-risk score, absence anomaly flags, late penalty rule, Roman Urdu.

**Not done / limits:** no React Native app; no face matching or liveness check; camera UI was not tested against a
real webcam (backend rules are tested); the AI red-team matrix has run against the offline engine and scripted/mocked
models, **not yet against a live hosted LLM** (run `python scripts/ai_redteam.py --live` with your key); no refresh
tokens (sessions last `JWT_EXPIRES_MINUTES`); notifications are in-app only; mid-month salary changes are not split.

## Project layout

```
hcm_lite/
  config.py clock.py context.py errors.py security.py db.py models.py main.py
  services/   org auth employees salary attendance leave payroll payslip_pdf analytics insights comms audit common
  ai/         service (agent + guardrails) tools providers offline nlu policy
  migrations/ Alembic env + versions (0001 baseline, 0002 auth hardening)
  api/        deps schemas routes/
  static/index.html
scripts/demo_harness.py   # notebook simulation: plays users through the real API (not part of the app)
scripts/ai_redteam.py     # adversarial AI evaluation (offline or --live)
tests/                    # 124 tests: rules, RBAC, auth, migrations, bulk setup, flows, analytics, payroll, AI
```
