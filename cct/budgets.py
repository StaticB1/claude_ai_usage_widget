from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from .store import Store


def _parse_resets_at(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def period_window(period: str,
                  now: Optional[datetime] = None,
                  resets_at: Optional[str] = None,
                  ) -> Tuple[datetime, datetime, str]:
    """(start, end, key) for a budget period anchored at `now` (UTC).

    For rolling Anthropic plan windows ('5h', '7d'), `resets_at` (from the
    cloud API) anchors the period so the key only changes when Anthropic
    actually resets the quota. Without it, falls back to a rolling window
    ending at `now`.
    """
    now = now or datetime.now(timezone.utc)
    if period == 'day':
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        key = start.strftime('%Y-%m-%d')
    elif period == 'week':
        weekday = now.weekday()
        day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        start = day - timedelta(days=weekday)
        end = start + timedelta(days=7)
        key = start.strftime('%G-W%V')
    elif period == 'month':
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        if now.month == 12:
            end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
        key = start.strftime('%Y-%m')
    elif period == '5h':
        end_dt = _parse_resets_at(resets_at) or now
        start = end_dt - timedelta(hours=5)
        end = end_dt
        key = end_dt.strftime('5h@%Y-%m-%dT%H:%M')
    elif period == '7d':
        end_dt = _parse_resets_at(resets_at) or now
        start = end_dt - timedelta(days=7)
        end = end_dt
        key = end_dt.strftime('7d@%Y-%m-%dT%H:%M')
    else:
        raise ValueError(f"Unknown period: {period}")
    return start, end, key


@dataclass
class BudgetState:
    id: int
    name: str
    scope: str
    period: str
    period_key: str
    spent_usd: float
    spent_tokens: int
    spent_pct: Optional[float]  # 0..100, only for utilization budgets
    limit_usd: Optional[float]
    limit_tokens: Optional[int]
    limit_pct: Optional[float]  # 0..100
    notify_at_pct: int
    last_notified_pct: int
    last_notified_period: Optional[str]
    data_available: bool = True  # False if utilization needs cloud data we lack

    @property
    def is_pct_based(self) -> bool:
        return self.limit_pct is not None

    @property
    def is_token_based(self) -> bool:
        return bool(self.limit_tokens) and not self.limit_usd \
            and self.limit_pct is None

    @property
    def pct(self) -> float:
        if self.limit_pct and self.spent_pct is not None:
            return min(self.spent_pct / self.limit_pct, 9.99)
        if self.limit_usd and self.limit_usd > 0:
            return min(self.spent_usd / self.limit_usd, 9.99)
        if self.limit_tokens and self.limit_tokens > 0:
            return min(self.spent_tokens / self.limit_tokens, 9.99)
        return 0.0

    def should_notify(self) -> bool:
        if self.is_pct_based and not self.data_available:
            return False
        pct = int(self.pct * 100)
        if self.last_notified_period != self.period_key:
            return pct >= self.notify_at_pct
        return pct >= self.notify_at_pct and pct > self.last_notified_pct


def _utilization_pct(usage_data: Optional[dict],
                     window: str) -> Optional[float]:
    """Read 0..100 utilization for '5h' or '7d' from a cloud-API payload."""
    if not usage_data:
        return None
    key = 'five_hour' if window == '5h' else 'seven_day'
    bucket = usage_data.get(key) or {}
    raw = bucket.get('utilization')
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    # Utilization from the usage API is already a percentage (0-100); use it
    # directly. The old `if v <= 1: v *= 100` heuristic turned genuine sub-1%
    # usage into 100% (issue #4).
    return max(0.0, min(v, 999.0))


def _resets_at(usage_data: Optional[dict], window: str) -> Optional[str]:
    if not usage_data:
        return None
    key = 'five_hour' if window == '5h' else 'seven_day'
    return (usage_data.get(key) or {}).get('resets_at')


def evaluate_budgets(store: Store,
                     now: Optional[datetime] = None,
                     usage_data: Optional[dict] = None) -> List[BudgetState]:
    now = now or datetime.now(timezone.utc)
    states: List[BudgetState] = []
    for b in store.list_budgets():
        scope = b['scope']
        period = b['period']
        limit_pct = b['limit_pct'] if 'limit_pct' in b.keys() else None

        if limit_pct is not None:
            spent_pct = _utilization_pct(usage_data, period)
            _, _, key = period_window(
                period, now, resets_at=_resets_at(usage_data, period))
            states.append(BudgetState(
                id=b['id'], name=b['name'], scope=scope, period=period,
                period_key=key,
                spent_usd=0.0, spent_tokens=0,
                spent_pct=spent_pct,
                limit_usd=None, limit_tokens=None,
                limit_pct=limit_pct,
                notify_at_pct=b['notify_at_pct'],
                last_notified_pct=b['last_notified_pct'],
                last_notified_period=b['last_notified_period'],
                data_available=spent_pct is not None,
            ))
            continue

        start, end, key = period_window(period, now)
        project: Optional[str] = None
        model: Optional[str] = None
        account: Optional[str] = None
        if scope.startswith('project:'):
            project = scope.split(':', 1)[1]
        elif scope.startswith('model:'):
            model = scope.split(':', 1)[1]
        elif scope.startswith('account:'):
            account = scope.split(':', 1)[1]
        # Clamp to the period's upper bound so future-dated rows (clock skew or
        # imported data) can't inflate the current period's spend.
        rows = store.query(since=start, until=end, project=project,
                           model=model, account=account)
        spent_usd = sum((r['cost_usd'] or 0) for r in rows)
        spent_tokens = sum(
            (r['input_tokens'] or 0)
            + (r['cache_creation_5m'] or 0)
            + (r['cache_creation_1h'] or 0)
            + (r['cache_read'] or 0)
            + (r['output_tokens'] or 0)
            for r in rows
        )
        states.append(BudgetState(
            id=b['id'],
            name=b['name'],
            scope=scope,
            period=period,
            period_key=key,
            spent_usd=spent_usd,
            spent_tokens=spent_tokens,
            spent_pct=None,
            limit_usd=b['limit_usd'],
            limit_tokens=b['limit_tokens'],
            limit_pct=None,
            notify_at_pct=b['notify_at_pct'],
            last_notified_pct=b['last_notified_pct'],
            last_notified_period=b['last_notified_period'],
        ))
    return states
