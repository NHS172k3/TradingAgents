"""US equity market trading-day checks.

This is a close approximation of the NYSE calendar: weekends, US federal
holidays (minus Columbus Day and Veterans Day, which the NYSE observes as
normal trading days) plus Good Friday (which the NYSE observes as closed but
is not a federal holiday). It does **not** account for early-close ("half")
days or one-off special closures (e.g. national days of mourning), and it
has no concept of intraday market hours.

For anything this approximation gets wrong, ``skip_dates`` in
``watchlist.yaml`` is the manual override: list any extra dates there and
:func:`is_trading_day` will treat them as non-trading days regardless of
what the calendar says.
"""

from __future__ import annotations

import datetime as _dt
from functools import lru_cache
from typing import Sequence

from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    USFederalHolidayCalendar,
)

# NYSE is open on Columbus Day and Veterans Day, so drop those federal
# holiday rules. NYSE is closed on Good Friday, which isn't a federal
# holiday, so add it.
_EXCLUDED_FEDERAL_HOLIDAYS = {"Columbus Day", "Veterans Day"}


class _NYSEHolidayCalendar(AbstractHolidayCalendar):
    """Approximate NYSE holiday calendar.

    Rules = US federal holidays, minus Columbus Day and Veterans Day, plus
    Good Friday.
    """

    rules = [
        rule
        for rule in USFederalHolidayCalendar.rules
        if rule.name not in _EXCLUDED_FEDERAL_HOLIDAYS
    ] + [GoodFriday]


@lru_cache(maxsize=None)
def _holidays(year: int) -> frozenset[_dt.date]:
    """Return the set of NYSE holiday dates that fall in or near `year`.

    Cached per year so repeated lookups don't recompute the calendar.
    """
    start = _dt.date(year, 1, 1)
    end = _dt.date(year, 12, 31)
    calendar = _NYSEHolidayCalendar()
    holidays = calendar.holidays(start=start, end=end)
    return frozenset(ts.date() for ts in holidays)


def is_trading_day(date_str: str, skip_dates: Sequence[str] = ()) -> bool:
    """Return True if `date_str` (YYYY-MM-DD) is a US equity trading day.

    False if the date is a Saturday/Sunday, appears in `skip_dates` (ISO
    date strings, used as a manual override from watchlist.yaml), or is a
    US equity market holiday per :class:`_NYSEHolidayCalendar`.
    """
    if date_str in skip_dates:
        return False

    date = _dt.date.fromisoformat(date_str)
    if date.weekday() >= 5:  # Saturday=5, Sunday=6
        return False

    if date in _holidays(date.year):
        return False

    return True


def next_trading_day(date_str: str, skip_dates: Sequence[str] = ()) -> str:
    """Return the next trading day strictly after `date_str` as YYYY-MM-DD."""
    date = _dt.date.fromisoformat(date_str)
    candidate = date + _dt.timedelta(days=1)
    while not is_trading_day(candidate.isoformat(), skip_dates=skip_dates):
        candidate += _dt.timedelta(days=1)
    return candidate.isoformat()


if __name__ == "__main__":
    import sys

    arg_date = sys.argv[1] if len(sys.argv) > 1 else _dt.date.today().isoformat()
    trading = is_trading_day(arg_date)
    status = "trading day" if trading else "NOT a trading day"
    print(f"{arg_date}: {status}")
    print(f"next trading day: {next_trading_day(arg_date)}")
