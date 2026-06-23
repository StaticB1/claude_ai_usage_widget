from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional


def fmt(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_full(n: int) -> str:
    return f"{n:,}"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hours}h {mins}m" if mins else f"{hours}h"


def fmt_cost(cost: float) -> str:
    if cost >= 100:
        return f"${cost:,.0f}"
    if cost >= 1:
        return f"${cost:.2f}"
    if cost >= 0.01:
        return f"${cost:.2f}"
    return f"${cost:.3f}"


def rel_time(ts: Optional[datetime]) -> str:
    if ts is None:
        return 'never'
    now = datetime.now(tz=ts.tzinfo or timezone.utc)
    secs = (now - ts).total_seconds()
    if secs < 60:
        return 'just now'
    if secs < 3600:
        return f'{int(secs // 60)}m ago'
    if secs < 86400:
        return f'{int(secs // 3600)}h ago'
    if secs < 7 * 86400:
        return f'{int(secs // 86400)}d ago'
    return ts.strftime('%b %d')


_WEEKDAY_SHORT = {0: 'Mo', 1: 'Tu', 2: 'We', 3: 'Th',
                  4: 'Fr', 5: 'Sa', 6: 'Su'}


def fmt_reset_countdown(iso: Optional[str]) -> str:
    """Compact countdown from now until ``iso`` (e.g. ``2h 15m``, ``3d 4h``)."""
    if not iso:
        return '—'
    try:
        end = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return iso
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    delta = (end - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return 'now'
    days, rem = divmod(int(delta), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def fmt_reset_absolute(iso: Optional[str], with_weekday: bool = False) -> str:
    """Local-time render of the reset moment.

    ``with_weekday=False`` → ``9:00P`` (suitable for the 5h window).
    ``with_weekday=True``  → ``Th 7:00P`` (suitable for the 7d window).
    """
    if not iso:
        return '—'
    try:
        end = datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone()
    except (ValueError, TypeError):
        return iso
    hour = end.hour % 12 or 12
    suffix = 'A' if end.hour < 12 else 'P'
    body = f"{hour}:{end.minute:02d}{suffix}"
    if with_weekday:
        return f"{_WEEKDAY_SHORT[end.weekday()]} {body}"
    return body


def period_range_text(period: str) -> str:
    now = datetime.now()
    if period == 'all':
        return 'since first recorded message'
    if period == 'today':
        return now.strftime('%b %d, %Y')
    deltas = {'5h': timedelta(hours=5),
              '7d': timedelta(days=7),
              '30d': timedelta(days=30)}
    delta = deltas.get(period)
    if delta is None:
        return ''
    start = now - delta
    if start.year == now.year:
        return f"{start.strftime('%b %d')} – {now.strftime('%b %d, %Y')}"
    return f"{start.strftime('%b %d, %Y')} – {now.strftime('%b %d, %Y')}"
