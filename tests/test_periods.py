"""Period cutoff tests.

The point of every test here is that calendar periods follow the user's own
calendar. A "Today" figure cut at UTC midnight but labelled with the local
date is wrong for as many hours as the machine is offset from UTC.
"""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from cct import periods
from cct.periods import (PERIOD_KEYS, UnknownPeriod, cutoff, cutoff_or_none,
                         local_day_start, local_month_end, local_month_start,
                         local_week_start, range_text)


class tz_seoul:
    """Run a block with the process in Asia/Seoul (UTC+9, no DST)."""

    def __enter__(self):
        self._prev = os.environ.get('TZ')
        os.environ['TZ'] = 'Asia/Seoul'
        time.tzset()

    def __exit__(self, *exc):
        if self._prev is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = self._prev
        time.tzset()


def test_all_has_no_cutoff():
    assert cutoff('all') is None


def test_rolling_windows_measure_back_from_now():
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    assert cutoff('5h', now) == now - timedelta(hours=5)
    assert cutoff('7d', now) == now - timedelta(days=7)
    assert cutoff('30d', now) == now - timedelta(days=30)


def test_today_is_the_local_day_not_the_utc_day():
    """08:00 in Seoul is still yesterday in UTC; "Today" must not be empty."""
    with tz_seoul():
        # 2026-08-20 08:00 KST == 2026-08-19 23:00 UTC.
        now = datetime(2026, 8, 19, 23, 0, tzinfo=timezone.utc)
        start = cutoff('today', now)
        # Local midnight that morning = 2026-08-19 15:00 UTC.
        assert start == datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc)
        assert start < now
        # The UTC-midnight version of this cutoff would have been 23:00 UTC,
        # i.e. after `now`, so every row in the local day fell outside it.
        assert start != datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)


def test_today_covers_a_message_sent_this_local_morning():
    with tz_seoul():
        now = datetime(2026, 8, 19, 23, 0, tzinfo=timezone.utc)  # 08:00 KST
        msg = datetime(2026, 8, 19, 22, 0, tzinfo=timezone.utc)  # 07:00 KST
        assert cutoff('today', now) <= msg


def test_naive_now_is_treated_as_utc():
    naive = datetime(2026, 8, 20, 12, 0)
    assert cutoff('5h', naive) == datetime(
        2026, 8, 20, 7, 0, tzinfo=timezone.utc)


def test_custom_day_and_hour_periods():
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    assert cutoff('3d', now) == now - timedelta(days=3)
    assert cutoff('36h', now) == now - timedelta(hours=36)


@pytest.mark.parametrize('bad', [
    'week', '', 'd', 'h', '-1d', '1.5d', 'xd', 'todayy',
    '²d',            # superscript two: isdigit() says yes, int() raises
    '999999999999d',      # would overflow timedelta
])
def test_unrecognised_periods_raise(bad):
    with pytest.raises(UnknownPeriod):
        cutoff(bad)


def test_cutoff_or_none_swallows_the_error():
    """The UI must not crash on a stale combo-box id."""
    assert cutoff_or_none('week') is None
    assert cutoff_or_none('5h') is not None


def test_every_documented_period_key_is_accepted():
    for key in PERIOD_KEYS:
        cutoff(key)  # must not raise


def test_local_day_start_is_local_midnight():
    with tz_seoul():
        when = periods.local_now().replace(hour=13, minute=45)
        start = local_day_start(when)
        assert (start.hour, start.minute, start.second) == (0, 0, 0)
        assert start.utcoffset() == timedelta(hours=9)


def test_week_starts_on_monday():
    with tz_seoul():
        # 2026-08-20 is a Thursday.
        thursday = datetime(2026, 8, 20, 13, 0).astimezone()
        monday = local_week_start(thursday)
        assert monday.weekday() == 0
        assert monday.day == 17
        # A Monday is its own week start.
        assert local_week_start(monday) == monday


def test_month_start_and_end():
    with tz_seoul():
        when = datetime(2026, 8, 20, 13, 0).astimezone()
        assert local_month_start(when).day == 1
        assert local_month_start(when).month == 8
        end = local_month_end(when)
        assert (end.year, end.month, end.day) == (2026, 9, 1)


def test_month_end_rolls_the_year():
    with tz_seoul():
        when = datetime(2026, 12, 31, 23, 30).astimezone()
        end = local_month_end(when)
        assert (end.year, end.month, end.day) == (2027, 1, 1)


def test_range_text():
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    assert range_text('all') == 'since first recorded message'
    assert '–' in range_text('7d', now)
    assert range_text('week', now) == ''
