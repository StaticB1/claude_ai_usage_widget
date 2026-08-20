"""Five-hour session windows.

The window Anthropic bills against is **not** derivable from the local Claude
Code logs, and that was the source of the widget disagreeing with what
``claude /usage`` shows. Two reasons:

* Usage from every surface counts — claude.ai in the browser, the desktop and
  mobile apps, other machines signed into the same account. A session can
  therefore already be open (or have just closed) with nothing about it in
  this machine's logs.
* The window start is a server-side fact. Measured against the live API: the
  local logs' first message was at 00:01:36Z, so first-message anchoring put
  the window end at 05:01:36Z, while the account's real window ended at
  05:30:00Z — a 28-minute error in the countdown, the burn rate and the ETA.

So: when the cloud API has told us when the window resets, that reset time is
the anchor and local rows are summed inside it. Local inference is the
fallback for when there is no token, the network is down, or the API is
rate-limiting — and it is clearly labelled as an estimate.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

BLOCK_HOURS = 5
BLOCK_DURATION = timedelta(hours=BLOCK_HOURS)

# How far back a caller should read rows when it wants the current session.
# Two window-lengths, so the fallback's boundary chain has a block of history
# to settle against instead of being anchored on whatever row the query
# cutoff happened to clip first.
SESSION_LOOKBACK = BLOCK_DURATION * 2


@dataclass
class Block:
    start: datetime              # window start
    end: datetime                # window end (start + BLOCK_HOURS)
    last_message: datetime
    messages: int
    input_tokens: int
    cache_creation: int
    cache_read: int
    output_tokens: int
    cost_usd: float
    # True when `start`/`end` came from the cloud API's resets_at, False when
    # they were inferred from local logs alone. The UI says which, because an
    # inferred window can be minutes off and must not be read as exact.
    anchored: bool = False

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.cache_creation
                + self.cache_read + self.output_tokens)

    def remaining(self, now: Optional[datetime] = None) -> timedelta:
        now = now or datetime.now(timezone.utc)
        return max(self.end - now, timedelta(0))

    def elapsed(self, now: Optional[datetime] = None) -> timedelta:
        """Wall-clock time the window has been open, clamped to its length."""
        now = now or datetime.now(timezone.utc)
        return min(max(now - self.start, timedelta(0)), BLOCK_DURATION)

    def is_active(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.start <= now < self.end


def parse_resets_at(iso: Optional[str]) -> Optional[datetime]:
    """Parse an API ``resets_at`` into an aware UTC datetime, or None."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _row_ts(row) -> Optional[datetime]:
    try:
        ts = row['timestamp']
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _num(row, key) -> float:
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return 0
    return v or 0


def _totals(bucket: Sequence) -> dict:
    return {
        'input_tokens': int(sum(_num(r, 'input_tokens') for r in bucket)),
        'cache_creation': int(sum(_num(r, 'cache_creation_5m')
                                  + _num(r, 'cache_creation_1h')
                                  for r in bucket)),
        'cache_read': int(sum(_num(r, 'cache_read') for r in bucket)),
        'output_tokens': int(sum(_num(r, 'output_tokens') for r in bucket)),
        'cost_usd': float(sum(_num(r, 'cost_usd') for r in bucket)),
    }


def compute_blocks(rows: Sequence) -> List[Block]:
    """Group messages into 5-hour windows inferred from the logs alone.

    A window opens at the first message and runs a fixed five hours; the next
    opens at the first message after that boundary. This is an estimate — see
    the module docstring for why the account's real windows can differ. Use
    ``active_session`` instead wherever cloud data may be available.
    """
    blocks: List[Block] = []
    parsed = sorted(
        (p for p in ((_row_ts(r), r) for r in rows) if p[0] is not None),
        key=lambda x: x[0],
    )
    if not parsed:
        return blocks

    block_start: Optional[datetime] = None
    bucket: List = []

    def flush():
        if not bucket or block_start is None:
            return
        blocks.append(Block(
            start=block_start,
            end=block_start + BLOCK_DURATION,
            last_message=bucket[-1][0],
            messages=len(bucket),
            anchored=False,
            **_totals([r for _, r in bucket]),
        ))

    for ts, r in parsed:
        if block_start is None or ts >= block_start + BLOCK_DURATION:
            flush()
            bucket = []
            block_start = ts
        bucket.append((ts, r))
    flush()
    return blocks


def anchored_session(rows: Sequence, resets_at,
                     now: Optional[datetime] = None) -> Optional[Block]:
    """The session window ending at ``resets_at``, filled from ``rows``.

    ``resets_at`` is the cloud API's own reset moment (an ISO string or a
    datetime), so the window is exactly the one the account is billed
    against. Rows outside it are ignored. Returns None if ``resets_at`` can't
    be read, or if the window has already closed.
    """
    end = resets_at if isinstance(resets_at, datetime) \
        else parse_resets_at(resets_at)
    if end is None:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    end = end.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    if end <= now:
        return None

    start = end - BLOCK_DURATION
    inside = sorted(
        (p for p in ((_row_ts(r), r) for r in rows)
         if p[0] is not None and start <= p[0] < end),
        key=lambda x: x[0],
    )
    return Block(
        start=start,
        end=end,
        last_message=inside[-1][0] if inside else start,
        messages=len(inside),
        anchored=True,
        **_totals([r for _, r in inside]),
    )


def active_session(rows: Sequence, resets_at=None,
                   now: Optional[datetime] = None) -> Optional[Block]:
    """The current 5-hour window, cloud-anchored when possible.

    Prefers the API's ``resets_at``; falls back to the last inferred block
    from ``rows`` when there is no cloud data. Returns None when no window is
    open. Check ``block.anchored`` to tell the two cases apart.
    """
    now = now or datetime.now(timezone.utc)
    block = anchored_session(rows, resets_at, now=now)
    if block is not None:
        return block
    blocks = compute_blocks(rows)
    if blocks and blocks[-1].is_active(now):
        return blocks[-1]
    return None


@dataclass
class Forecast:
    burn_rate_per_min_tokens: float
    burn_rate_per_min_cost: float
    eta_block_end: Optional[timedelta]
    eta_to_limit: Optional[timedelta]
    block: Optional[Block]


def forecast(block: Optional[Block],
             now: Optional[datetime] = None,
             cloud_5h_pct: Optional[float] = None,
             limit_pct: float = 100.0) -> Forecast:
    """Burn rate and ETAs for one session window.

    Both the token burn rate and the plan-utilization projection measure
    against the same elapsed time — the window's open duration. They used to
    use different bases (wall-clock for one, time-to-last-message for the
    other), which made the ETA disagree with the burn rate it was supposedly
    derived from.

    ``cloud_5h_pct`` is a 0..1 plan utilization; when given, the forecast also
    projects when ``limit_pct`` is crossed at the current pace.
    """
    now = now or datetime.now(timezone.utc)
    if block is None or not block.is_active(now):
        return Forecast(0.0, 0.0, None, None, block)

    elapsed_min = max(block.elapsed(now).total_seconds() / 60, 1.0)
    rate_tokens = block.total_tokens / elapsed_min
    rate_cost = block.cost_usd / elapsed_min

    eta_to_limit: Optional[timedelta] = None
    if cloud_5h_pct is not None and cloud_5h_pct > 0:
        pct_now = cloud_5h_pct * 100
        if pct_now >= limit_pct:
            eta_to_limit = timedelta(0)
        else:
            pct_per_min = pct_now / elapsed_min
            if pct_per_min > 0:
                eta_to_limit = timedelta(
                    minutes=(limit_pct - pct_now) / pct_per_min)

    return Forecast(
        burn_rate_per_min_tokens=rate_tokens,
        burn_rate_per_min_cost=rate_cost,
        eta_block_end=block.remaining(now),
        eta_to_limit=eta_to_limit,
        block=block,
    )


def forecast_active(blocks: List[Block],
                    now: Optional[datetime] = None,
                    cloud_5h_pct: Optional[float] = None,
                    limit_pct: float = 100.0) -> Forecast:
    """``forecast`` over the last block of a ``compute_blocks`` list."""
    now = now or datetime.now(timezone.utc)
    if not blocks:
        return Forecast(0.0, 0.0, None, None, None)
    return forecast(blocks[-1], now=now, cloud_5h_pct=cloud_5h_pct,
                    limit_pct=limit_pct)
