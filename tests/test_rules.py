"""Pure business-rule tests (no HTTP): attendance status, calendar, tax slab, payslip maths, geofence."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from hcm_lite.errors import ValidationFailed
from hcm_lite.services.attendance import compute_status, haversine_m
from hcm_lite.services.common import WorkCalendar, resolve_period
from hcm_lite.services.payroll import AttendanceFacts, annual_tax, calculate_payslip
from hcm_lite.services.salary import compute_structure

RULES = {"office_start": "09:00", "grace_minutes": 15, "half_day_hours": 5.0}


@pytest.mark.parametrize("hhmm,expected_late", [("08:55", False), ("09:15", False), ("09:15:59", False), ("09:16", True)])
def test_late_after_grace(hhmm, expected_late):
    parts = [int(x) for x in hhmm.split(":")]
    check_in = datetime(2026, 10, 1, parts[0], parts[1], parts[2] if len(parts) > 2 else 0)
    status, is_late, _ = compute_status(check_in, None, RULES)
    assert is_late is expected_late
    assert status == ("late" if expected_late else "present")


def test_half_day_under_five_hours_overrides_status():
    cin = datetime(2026, 10, 1, 9, 30)
    status, is_late, worked = compute_status(cin, datetime(2026, 10, 1, 14, 29), RULES)
    assert (status, is_late, worked) == ("half_day", True, 299)
    status, _, worked = compute_status(cin, datetime(2026, 10, 1, 14, 30), RULES)
    assert (status, worked) == ("late", 300)


def test_work_calendar_skips_weekends_and_holidays():
    cal = WorkCalendar(weekend_days=frozenset({5, 6}), holidays={date(2026, 11, 9): "Iqbal Day"})
    assert cal.count(date(2026, 10, 1), date(2026, 10, 31)) == 22
    assert cal.count(date(2026, 11, 6), date(2026, 11, 10)) == 2  # Fri, (Sat, Sun skipped), (Mon holiday), Tue


def test_resolve_period():
    today = date(2026, 10, 23)
    assert resolve_period("this_month", today) == (date(2026, 10, 1), date(2026, 10, 31))
    assert resolve_period("last_month", today) == (date(2026, 9, 1), date(2026, 9, 30))
    assert resolve_period("this_week", today) == (date(2026, 10, 19), date(2026, 10, 25))


@pytest.mark.parametrize("annual,tax", [
    (0, 0), (600_000, 0), (900_000, 15_000), (1_200_000, 30_000), (1_800_000, 120_000),
    (2_400_000, 210_000), (3_000_000, 360_000), (3_600_000, 510_000), (4_000_000, 650_000),
])
def test_tax_slab_boundaries(annual, tax):
    assert annual_tax(annual) == Decimal(tax)


def test_salary_structure_gross_is_one_and_a_half_basic():
    s = compute_structure(100_000)
    assert (s["hra"], s["medical"], s["gross"]) == (Decimal("40000.00"), Decimal("10000.00"), Decimal("150000.00"))
    with pytest.raises(ValidationFailed):
        compute_structure(0)


def test_payslip_deducts_absent_unpaid_and_half_days():
    s = compute_structure(100_000)
    calc = calculate_payslip(**s, working_days=22, eligible_days=22,
                             facts=AttendanceFacts(absent_days=1, unpaid_leave_days=1, half_days=2, late_count=4))
    assert calc.prorated_gross == Decimal("150000.00")
    assert calc.deduction_days == 3.0  # 1 absent + 1 unpaid + 0.5 x 2 half days
    assert calc.deductions == Decimal("20454.55")  # 150000 / 22 * 3
    assert calc.tax == Decimal("10000.00")  # (30k + 15% x 600k) / 12
    assert calc.net == Decimal("119545.45")
    assert calc.late_penalty_days == 0.0


def test_tax_never_exceeds_pay_left_after_deductions():
    s = compute_structure(100_000)
    calc = calculate_payslip(**s, working_days=22, eligible_days=22, facts=AttendanceFacts(absent_days=21.5))
    assert calc.net >= 0 and calc.prorated_gross - calc.deductions - calc.tax == calc.net
    assert calc.tax == calc.prorated_gross - calc.deductions  # capped


def test_late_penalty_rule_three_lates_half_day():
    s = compute_structure(100_000)
    calc = calculate_payslip(**s, working_days=22, eligible_days=22, facts=AttendanceFacts(late_count=7),
                             late_penalty_enabled=True)
    assert calc.late_penalty_days == 1.0  # 7 lates -> 2 x 0.5 day


def test_joiner_is_prorated_by_eligible_working_days():
    s = compute_structure(100_000)
    calc = calculate_payslip(**s, working_days=22, eligible_days=15, facts=AttendanceFacts())
    assert calc.prorated_gross == Decimal("102272.73")
    assert calc.taxable_annual == Decimal("1227272.76")
    assert calc.tax == Decimal("2840.91")


def test_haversine_distance():
    # ~111 m per 0.001 deg latitude
    assert 105 < haversine_m(24.8607, 67.0011, 24.8617, 67.0011) < 115
