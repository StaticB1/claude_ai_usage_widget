import os
from datetime import datetime, timedelta, timezone

from cct.format import fmt_reset_absolute, fmt_reset_countdown


def test_countdown_hours_minutes():
    end = datetime.now(timezone.utc) + timedelta(hours=2, minutes=15, seconds=30)
    assert fmt_reset_countdown(end.isoformat()) == '2h 15m'


def test_countdown_days():
    end = datetime.now(timezone.utc) + timedelta(days=3, hours=4, minutes=5)
    assert fmt_reset_countdown(end.isoformat()) == '3d 4h'


def test_countdown_minutes_only():
    end = datetime.now(timezone.utc) + timedelta(minutes=30, seconds=30)
    assert fmt_reset_countdown(end.isoformat()) == '30m'


def test_countdown_already_elapsed():
    end = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert fmt_reset_countdown(end.isoformat()) == 'now'


def test_countdown_handles_missing():
    assert fmt_reset_countdown(None) == '—'
    assert fmt_reset_countdown('') == '—'


def test_absolute_5h_format(monkeypatch):
    monkeypatch.setenv('TZ', 'UTC')
    if hasattr(__import__('time'), 'tzset'):
        __import__('time').tzset()
    out = fmt_reset_absolute('2026-05-01T21:00:00+00:00', with_weekday=False)
    assert out == '9:00P'


def test_absolute_7d_format(monkeypatch):
    monkeypatch.setenv('TZ', 'UTC')
    if hasattr(__import__('time'), 'tzset'):
        __import__('time').tzset()
    # 2026-05-07 is a Thursday.
    out = fmt_reset_absolute('2026-05-07T19:00:00+00:00', with_weekday=True)
    assert out == 'Th 7:00P'


def test_absolute_morning(monkeypatch):
    monkeypatch.setenv('TZ', 'UTC')
    if hasattr(__import__('time'), 'tzset'):
        __import__('time').tzset()
    out = fmt_reset_absolute('2026-05-01T09:30:00+00:00')
    assert out == '9:30A'


def test_absolute_midnight(monkeypatch):
    monkeypatch.setenv('TZ', 'UTC')
    if hasattr(__import__('time'), 'tzset'):
        __import__('time').tzset()
    out = fmt_reset_absolute('2026-05-01T00:15:00+00:00')
    assert out == '12:15A'
