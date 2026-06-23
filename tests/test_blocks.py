from datetime import datetime, timedelta, timezone

from cct.blocks import BLOCK_HOURS, Block, compute_blocks, forecast_active


def _row(ts, inp=100, out=200, cc5=0, cc1=0, cr=0, cost=0.01):
    return {
        'timestamp': ts.isoformat(),
        'input_tokens': inp,
        'cache_creation_5m': cc5,
        'cache_creation_1h': cc1,
        'cache_read': cr,
        'output_tokens': out,
        'cost_usd': cost,
    }


def test_empty():
    assert compute_blocks([]) == []


def test_single_block_when_messages_within_5h():
    base = datetime(2026, 4, 28, 8, tzinfo=timezone.utc)
    rows = [_row(base + timedelta(hours=i)) for i in (0, 1, 4)]
    blocks = compute_blocks(rows)
    assert len(blocks) == 1
    assert blocks[0].messages == 3
    assert blocks[0].start == base
    assert blocks[0].end == base + timedelta(hours=BLOCK_HOURS)


def test_new_block_when_gap_exceeds_5h():
    base = datetime(2026, 4, 28, 8, tzinfo=timezone.utc)
    rows = [
        _row(base),
        _row(base + timedelta(hours=2)),
        _row(base + timedelta(hours=6)),  # past first block boundary
        _row(base + timedelta(hours=8)),
    ]
    blocks = compute_blocks(rows)
    assert len(blocks) == 2
    assert blocks[0].messages == 2
    assert blocks[1].messages == 2
    assert blocks[1].start == base + timedelta(hours=6)


def test_block_aggregates_tokens_and_cost():
    base = datetime(2026, 4, 28, 8, tzinfo=timezone.utc)
    rows = [
        _row(base, inp=100, out=200, cc5=10, cr=5, cost=0.5),
        _row(base + timedelta(minutes=10), inp=50, out=100,
             cc5=20, cr=10, cost=0.25),
    ]
    b = compute_blocks(rows)[0]
    assert b.input_tokens == 150
    assert b.output_tokens == 300
    assert b.cache_creation == 30
    assert b.cache_read == 15
    assert b.cost_usd == 0.75
    assert b.total_tokens == 150 + 30 + 15 + 300


def test_active_block_remaining_decreases_with_now():
    base = datetime(2026, 4, 28, 8, tzinfo=timezone.utc)
    blocks = compute_blocks([_row(base)])
    b = blocks[0]
    assert b.is_active(now=base + timedelta(hours=1))
    assert not b.is_active(now=base + timedelta(hours=BLOCK_HOURS))


def test_forecast_no_active_block():
    base = datetime(2026, 4, 28, 8, tzinfo=timezone.utc)
    blocks = compute_blocks([_row(base)])
    fc = forecast_active(blocks, now=base + timedelta(hours=10))
    assert fc.burn_rate_per_min_tokens == 0
    assert fc.eta_block_end is None


def test_forecast_eta_to_limit_with_cloud_pct():
    """At 50% utilization 60 minutes in, ETA to 100% should be ~60 min."""
    base = datetime(2026, 4, 28, 8, tzinfo=timezone.utc)
    rows = [_row(base + timedelta(minutes=i)) for i in (0, 30, 60)]
    blocks = compute_blocks(rows)
    now = base + timedelta(minutes=60)
    fc = forecast_active(blocks, now=now, cloud_5h_pct=0.50)
    assert fc.eta_to_limit is not None
    minutes = fc.eta_to_limit.total_seconds() / 60
    assert 50 < minutes < 70  # ~60min, with some tolerance
