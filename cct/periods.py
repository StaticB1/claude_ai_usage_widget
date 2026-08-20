"""Period cutoffs, shared by the CLI and the GUI.

Two rules this module exists to enforce:

1. **Calendar periods are local.** ``today`` / ``week`` / ``month`` are the
   user's calendar day, week and month — not UTC's. The dashboard labels
   "Today" with the local date, so cutting at UTC midnight showed the wrong
   day's numbers for every hour of the local/UTC offset (9 hours in Seoul).
   Cutoffs are computed in local time and converted to UTC only for the
   query.
2. **One definition.** The CLI and GUI each used to carry their own copy of
   this logic with different behaviour (one raised on an unknown period, the
   other silently fell back to all-time), so the same period name produced
   different numbers in the two front ends.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional

# Period keys the dashboard and CLI both accept.
PERIOD_KEYS = ('all', 'today', '5h', '7d', '30d')


class UnknownPeriod(ValueError):
    pass


def local_now() -> datetime:
    """Timezone-aware 'now' in the machine's local zone."""
    return datetime.now(timezone.utc).astimezone()


def local_day_start(when: Optional[datetime] = None) -> datetime:
    """Midnight at the start of ``when``'s local day, as an aware datetime."""
    when = when or local_now()
    if when.tzinfo is None:
        when = when.astimezone()
    return when.replace(hour=0, minute=0, second=0, microsecond=0)


def local_week_start(when: Optional[datetime] = None) -> datetime:
    """Local midnight on the Monday of ``when``'s week."""
    day = local_day_start(when)
    return day - timedelta(days=day.weekday())


def local_month_start(when: Optional[datetime] = None) -> datetime:
    """Local midnight on the 1st of ``when``'s month."""
    day = local_day_start(when)
    return day.replace(day=1)


def local_month_end(when: Optional[datetime] = None) -> datetime:
    """Local midnight on the 1st of the *following* month."""
    start = local_month_start(when)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def cutoff(period: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Start of ``period`` as an aware UTC datetime, or None for all-time.

    Accepts the named periods in ``PERIOD_KEYS`` plus custom ``<N>d`` /
    ``<N>h`` forms. Raises ``UnknownPeriod`` for anything else — callers that
    want a lenient fallback should catch it rather than relying on a silent
    None.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if period == 'all':
        return None
    if period == 'today':
        return local_day_start(now.astimezone()).astimezone(timezone.utc)
    if period == '5h':
        return now - timedelta(hours=5)
    if period == '7d':
        return now - timedelta(days=7)
    if period == '30d':
        return now - timedelta(days=30)

    # Custom "<N>d" / "<N>h". isascii()+isdecimal() rather than isdigit(),
    # which accepts superscripts and other Unicode digits that then crash
    # int(); the cap stops an enormous N raising OverflowError out of
    # timedelta.
    if period and period[-1] in ('d', 'h'):
        num = period[:-1]
        if num.isascii() and num.isdecimal() and int(num) <= 100_000:
            unit = 'days' if period[-1] == 'd' else 'hours'
            return now - timedelta(**{unit: int(num)})
    raise UnknownPeriod(f"Unknown period: {period}")


def cutoff_or_none(period: str,
                   now: Optional[datetime] = None) -> Optional[datetime]:
    """``cutoff`` that treats an unrecognised period as all-time.

    For UI code paths where a stale combo-box id must not raise.
    """
    try:
        return cutoff(period, now)
    except UnknownPeriod:
        return None


def range_text(period: str, now: Optional[datetime] = None) -> str:
    """Human-readable date range for a period, in local time."""
    now = (now or local_now())
    if now.tzinfo is None:
        now = now.astimezone()
    else:
        now = now.astimezone()
    if period == 'all':
        return 'since first recorded message'
    if period == 'today':
        return now.strftime('%b %d, %Y')
    start_utc = cutoff_or_none(period, now.astimezone(timezone.utc))
    if start_utc is None:
        return ''
    start = start_utc.astimezone()
    if start.year == now.year:
        return f"{start.strftime('%b %d')} – {now.strftime('%b %d, %Y')}"
    return f"{start.strftime('%b %d, %Y')} – {now.strftime('%b %d, %Y')}"
