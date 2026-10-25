"""Shared helpers: company settings, working-day calendar, periods and JSON-safe snapshots."""

from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from ..errors import NotFound, ValidationFailed
from ..models import Company, Holiday


def get_company(db: Session) -> Company:
    company = db.scalar(select(Company).limit(1))
    if company is None:
        raise NotFound("Company is not set up yet — run first-run setup", code="not_initialized")
    return company


def parse_hhmm(value: str) -> time:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise ValidationFailed(f"Invalid time '{value}', expected HH:MM") from exc


def weekend_set(company: Company) -> frozenset[int]:
    return frozenset(int(x) for x in company.weekend_days.split(",") if x.strip() != "")


@dataclass(frozen=True)
class WorkCalendar:
    """Working-day arithmetic: skips weekends (company setting) and public holidays."""

    weekend_days: frozenset[int]
    holidays: dict[date, str]
    office_start: time = time(9, 0)

    def tracking_start(self, join_date: date, registered_at: datetime | None) -> date:
        """First day absences can count: the later of the join date and the day the employee was
        registered (the next day if registered after office start). No login = no absence."""
        if registered_at is None:
            return join_date
        first = registered_at.date()
        if registered_at.time() > self.office_start:
            first += timedelta(days=1)
        return max(join_date, first)

    def is_weekend(self, d: date) -> bool:
        return d.weekday() in self.weekend_days

    def is_holiday(self, d: date) -> bool:
        return d in self.holidays

    def is_working_day(self, d: date) -> bool:
        return not self.is_weekend(d) and not self.is_holiday(d)

    def working_days(self, start: date, end: date) -> list[date]:
        if end < start:
            return []
        days, d = [], start
        while d <= end:
            if self.is_working_day(d):
                days.append(d)
            d += timedelta(days=1)
        return days

    def count(self, start: date, end: date) -> int:
        return len(self.working_days(start, end))


def load_calendar(db: Session) -> WorkCalendar:
    company = get_company(db)
    holidays = {h.day: h.name for h in db.scalars(select(Holiday))}
    return WorkCalendar(weekend_days=weekend_set(company), holidays=holidays, office_start=parse_hhmm(company.office_start))


# ------------------------------------------------------------------ periods
def month_bounds(month: str) -> tuple[date, date]:
    try:
        year, mon = (int(x) for x in month.split("-"))
        last = _cal.monthrange(year, mon)[1]
        return date(year, mon, 1), date(year, mon, last)
    except (ValueError, AttributeError) as exc:
        raise ValidationFailed(f"Invalid month '{month}', expected YYYY-MM") from exc


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


PERIODS = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "this_year")


def resolve_period(period: str | None, today: date) -> tuple[date, date]:
    period = (period or "this_month").lower()
    if period == "today":
        return today, today
    if period == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if period == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if period == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)
    if period == "this_month":
        return month_bounds(month_key(today))
    if period == "last_month":
        first = today.replace(day=1) - timedelta(days=1)
        return month_bounds(month_key(first))
    if period == "this_year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    raise ValidationFailed(f"Unknown period '{period}'. Use one of: {', '.join(PERIODS)}")


def resolve_range(
    today: date, date_from: date | None = None, date_to: date | None = None, period: str | None = None
) -> tuple[date, date]:
    if date_from or date_to:
        start = date_from or date_to
        end = date_to or date_from
        assert start is not None and end is not None
        if end < start:
            raise ValidationFailed("date_to must be on or after date_from")
        return start, end
    return resolve_period(period, today)


# ------------------------------------------------------------------ serialization
def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return value


def snapshot(obj: Any, exclude: Iterable[str] = ("password_hash",)) -> dict[str, Any]:
    """Column values of an ORM object as a JSON-safe dict (used for audit before/after)."""
    excluded = set(exclude)
    mapper = inspect(obj).mapper
    return {
        col.key: jsonable(getattr(obj, col.key))
        for col in mapper.column_attrs
        if col.key not in excluded
    }


def paginate(page: int, size: int) -> tuple[int, int]:
    page = max(1, page)
    size = max(1, min(size, 200))
    return page, size
