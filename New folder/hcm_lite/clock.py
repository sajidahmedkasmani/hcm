"""Injectable clock. The server stamps every time (attendance, approvals) from here.

`SystemClock` is used in production; `FrozenClock` makes tests and the notebook
simulation deterministic without touching business logic.
All datetimes are *naive local time* in the company timezone.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime: ...

    def today(self) -> date: ...


class SystemClock:
    def __init__(self, timezone: str = "Asia/Karachi") -> None:
        self._tz = ZoneInfo(timezone)

    def now(self) -> datetime:
        return datetime.now(self._tz).replace(tzinfo=None, microsecond=0)

    def today(self) -> date:
        return self.now().date()


class FrozenClock:
    def __init__(self, at: datetime) -> None:
        self._now = at

    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return self._now.date()

    def set(self, at: datetime) -> None:
        self._now = at

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)
