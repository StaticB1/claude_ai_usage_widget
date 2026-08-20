from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from .blocks import BLOCK_DURATION, parse_resets_at
from .cloud import utilization_pct
from .periods import (local_day_start, local_month_end, local_month_start,
                     local_week_start)
from .store import Store


def period_window(period: str,
                  now: Optional[datetime] = None,
                  resets_at: Optional[str] = None,
                  ) -> Tuple[datetime, datetime, str]:
    """(start, end, key) for a budget period anchored at `now` (UTC).

    Calendar periods ('day', 'week', 'month') follow the user's **local**
    calendar — a monthly cap should roll over at local midnight on the 1st,
    not at whatever local hour UTC midnight happens to fall on. Boundaries are
    computed locally and returned in UTC for querying.

    Rolling Anthropic plan windows ('5h', '7d') are anchored on `resets_at`
    from the cloud API, so the key only changes when Anthropic actually resets
    the quota. Without it, falls back to a rolling window ending at `now`.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone()

    if period == 'day':
        start_l = local_day_start(local)
        end_l = start_l + timedelta(days=1)
        key = start_l.strftime('%Y-%m-%d')
    elif period == 'week':
        start_l = local_week_start(local)
        end_l = start_l + timedelta(days=7)
        key = start_l.strftime('%G-W%V')
    elif period == 'month':
        start_l = local_month_start(local)
        end_l = local_month_end(local)
        key = start_l.strftime('%Y-%m')
    elif period in ('5h', '7d'):
        end_dt = parse_resets_at(resets_at) or now
        span = BLOCK_DURATION if period == '5h' else timedelta(days=7)
        return (end_dt - span, end_dt,
                end_dt.strftime(f'{period}@%Y-%m-%dT%H:%M'))
    else:
        raise ValueError(f"Unknown period: {period}")
    return (start_l.astimezone(timezone.utc),
            end_l.astimezone(timezone.utc), key)


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
    bucket = usage_data.get(key)
    if not isinstance(bucket, dict):
        return None
    return utilization_pct(bucket.get('utilization'))


def _resets_at(usage_data: Optional[dict], window: str) -> Optional[str]:
    if not usage_data:
        return None
    key = 'five_hour' if window == '5h' else 'seven_day'
    bucket = usage_data.get(key)
    return (bucket or {}).get('resets_at') if isinstance(bucket, dict) else None


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
        # imported data) can't inflate the current period's spend. Summed in
        # SQL — this runs on every poll tick, and pulling a month of rows into
        # Python to add two columns stalled the interface.
        agg = store.totals(since=start, until=end, project=project,
                           model=model, account=account)
        spent_usd = float(agg.get('cost_usd') or 0)
        spent_tokens = int(agg.get('total_tokens') or 0)
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
