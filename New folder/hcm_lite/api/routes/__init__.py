from . import ai, attendance, auth, comms, dashboards, employees, leave, org, payroll

ROUTERS = [auth.router, org.router, employees.router, attendance.router, leave.router, payroll.router,
           dashboards.router, comms.router, ai.router]
