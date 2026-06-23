from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

BLOCK_HOURS = 5


@dataclass
class Block:
    start: datetime              # first message in the block
    end: datetime                # planned end (start + BLOCK_HOURS)
    last_message: datetime
    messages: int
    input_tokens: int
    cache_creation: int
    cache_read: int
    output_tokens: int
    cost_usd: float

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.cache_creation
                + self.cache_read + self.output_tokens)

    @property
    def elapsed(self) -> timedelta:
        return self.last_message - self.start

    def remaining(self, now: Optional[datetime] = None) -> timedelta:
        now = now or datetime.now(timezone.utc)
        return max(self.end - now, timedelta(0))

    def is_active(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now < self.end


def _row_ts(row) -> datetime:
    ts = row['timestamp']
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def compute_blocks(rows: Sequence) -> List[Block]:
    """Group messages into Anthropic-style 5-hour rolling blocks.

    A block starts at the first message and lasts BLOCK_HOURS regardless of
    activity. The next block starts at the first message after that boundary
    — matching what `claude /usage` displays.
    """
    blocks: List[Block] = []
    if not rows:
        return blocks

    parsed = sorted(((_row_ts(r), r) for r in rows), key=lambda x: x[0])

    block_start: Optional[datetime] = None
    bucket: List[tuple] = []

    def flush():
        if not bucket or block_start is None:
            return
        inp = sum(r['input_tokens'] for _, r in bucket)
        cc = sum((r['cache_creation_5m'] or 0)
                 + (r['cache_creation_1h'] or 0) for _, r in bucket)
        cr = sum(r['cache_read'] or 0 for _, r in bucket)
        out = sum(r['output_tokens'] or 0 for _, r in bucket)
        cost = sum(r['cost_usd'] or 0 for _, r in bucket)
        last_ts = bucket[-1][0]
        blocks.append(Block(
            start=block_start,
            end=block_start + timedelta(hours=BLOCK_HOURS),
            last_message=last_ts,
            messages=len(bucket),
            input_tokens=inp,
            cache_creation=cc,
            cache_read=cr,
            output_tokens=out,
            cost_usd=cost,
        ))

    for ts, r in parsed:
        if block_start is None or ts >= block_start + timedelta(hours=BLOCK_HOURS):
            flush()
            bucket = []
            block_start = ts
        bucket.append((ts, r))
    flush()
    return blocks


@dataclass
class Forecast:
    burn_rate_per_min_tokens: float
    burn_rate_per_min_cost: float
    eta_block_end: Optional[timedelta]
    eta_to_limit: Optional[timedelta]
    block: Optional[Block]


def forecast_active(blocks: List[Block],
                    now: Optional[datetime] = None,
                    cloud_5h_pct: Optional[float] = None,
                    limit_pct: float = 100.0) -> Forecast:
    """Return burn-rate and ETAs for the active 5h block.

    If `cloud_5h_pct` (0..1 utilization from claude.ai) is provided we also
    project when the user crosses `limit_pct` at the current burn rate.
    """
    now = now or datetime.now(timezone.utc)
    if not blocks:
        return Forecast(0.0, 0.0, None, None, None)
    block = blocks[-1]
    if not block.is_active(now):
        return Forecast(0.0, 0.0, None, None, block)

    elapsed_min = max((now - block.start).total_seconds() / 60, 1.0)
    rate_tokens = block.total_tokens / elapsed_min
    rate_cost = block.cost_usd / elapsed_min
    eta_end = block.remaining(now)

    eta_to_limit: Optional[timedelta] = None
    if cloud_5h_pct is not None and cloud_5h_pct > 0:
        pct_now = cloud_5h_pct * 100
        if pct_now < limit_pct:
            elapsed_active = max(
                (block.last_message - block.start).total_seconds() / 60,
                1.0,
            )
            pct_per_min = pct_now / elapsed_active
            if pct_per_min > 0:
                minutes = (limit_pct - pct_now) / pct_per_min
                eta_to_limit = timedelta(minutes=minutes)
        else:
            eta_to_limit = timedelta(0)

    return Forecast(
        burn_rate_per_min_tokens=rate_tokens,
        burn_rate_per_min_cost=rate_cost,
        eta_block_end=eta_end,
        eta_to_limit=eta_to_limit,
        block=block,
    )
