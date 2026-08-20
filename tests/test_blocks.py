from datetime import datetime, timedelta, timezone

from cct.blocks import (BLOCK_HOURS, active_session, anchored_session,
                        compute_blocks, forecast, forecast_active,
                        parse_resets_at)


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


# ── Cloud-anchored windows ───────────────────────────────────────────────────
# The bug these cover: the widget inferred the 5-hour window from the first
# message in the local logs, so it disagreed with `claude /usage`. Measured
# once against the live API, the local guess ended at 05:01:36Z and the real
# window ended at 05:30:00Z. Anchoring on the API's resets_at removes the
# error entirely, because that value *is* the window end.


def test_anchored_window_ends_exactly_at_resets_at():
    resets = datetime(2026, 8, 20, 5, 30, tzinfo=timezone.utc)
    now = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
    rows = [_row(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc))]
    b = anchored_session(rows, resets.isoformat(), now=now)
    assert b is not None
    assert b.end == resets
    assert b.start == resets - timedelta(hours=BLOCK_HOURS)
    assert b.anchored is True


def test_anchored_window_ignores_the_first_local_message():
    """The measured failure: local logs start 28 minutes before the real
    window boundary, so first-message anchoring lands in the wrong place."""
    resets = datetime(2026, 8, 20, 5, 30, tzinfo=timezone.utc)
    first_local = datetime(2026, 8, 20, 0, 1, 36, tzinfo=timezone.utc)
    rows = [_row(first_local), _row(first_local + timedelta(hours=1))]
    now = datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc)

    inferred = compute_blocks(rows)[-1]
    assert inferred.end == first_local + timedelta(hours=BLOCK_HOURS)

    b = anchored_session(rows, resets, now=now)
    assert b.end == resets
    assert b.end - inferred.end == timedelta(minutes=28, seconds=24)


def test_anchored_window_excludes_rows_outside_it():
    resets = datetime(2026, 8, 20, 5, 30, tzinfo=timezone.utc)
    now = datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc)
    rows = [
        _row(datetime(2026, 8, 20, 0, 15, tzinfo=timezone.utc), cost=1.0),
        _row(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc), cost=2.0),
        _row(datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc), cost=4.0),
    ]
    b = anchored_session(rows, resets, now=now)
    # 00:15 is before the 00:30 window start, so it is not this session's.
    assert b.messages == 2
    assert b.cost_usd == 6.0
    assert b.last_message == datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)


def test_anchored_window_with_no_local_rows_is_still_the_real_window():
    """Usage from the browser or another machine opens a window this machine
    has no logs for. The countdown must still be right, at zero local spend."""
    resets = datetime(2026, 8, 20, 5, 30, tzinfo=timezone.utc)
    now = datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)
    b = anchored_session([], resets, now=now)
    assert b is not None
    assert b.messages == 0
    assert b.total_tokens == 0
    assert b.remaining(now) == timedelta(hours=1, minutes=30)


def test_anchored_returns_none_for_a_closed_or_unreadable_window():
    now = datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc)
    past = datetime(2026, 8, 20, 5, 30, tzinfo=timezone.utc)
    assert anchored_session([], past, now=now) is None
    assert anchored_session([], None, now=now) is None
    assert anchored_session([], 'not a date', now=now) is None


def test_parse_resets_at_accepts_z_and_offset_and_naive():
    want = datetime(2026, 8, 20, 5, 30, tzinfo=timezone.utc)
    assert parse_resets_at('2026-08-20T05:30:00Z') == want
    assert parse_resets_at('2026-08-20T14:30:00+09:00') == want
    assert parse_resets_at('2026-08-20T05:30:00') == want
    assert parse_resets_at('') is None
    assert parse_resets_at(None) is None
    assert parse_resets_at('nonsense') is None


def test_active_session_prefers_the_cloud_anchor():
    resets = datetime(2026, 8, 20, 5, 30, tzinfo=timezone.utc)
    now = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
    rows = [_row(datetime(2026, 8, 20, 0, 1, 36, tzinfo=timezone.utc))]
    b = active_session(rows, resets, now=now)
    assert b.anchored is True
    assert b.end == resets


def test_active_session_falls_back_to_local_inference():
    """No cloud data: still give an answer, but flag it as an estimate so the
    UI can say so instead of presenting a guess as the billed window."""
    base = datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc)
    now = base + timedelta(hours=1)
    b = active_session([_row(base)], None, now=now)
    assert b is not None
    assert b.anchored is False
    assert b.start == base


def test_active_session_returns_none_when_nothing_is_open():
    base = datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc)
    now = base + timedelta(hours=9)
    assert active_session([_row(base)], None, now=now) is None
    assert active_session([], None, now=now) is None


def test_forecast_uses_one_elapsed_basis_for_rate_and_eta():
    """Burn rate and ETA-to-limit used to measure against different clocks
    (wall-clock vs time-to-last-message), so the ETA contradicted the rate it
    was supposedly derived from."""
    resets = datetime(2026, 8, 20, 5, 30, tzinfo=timezone.utc)
    start = resets - timedelta(hours=BLOCK_HOURS)
    # 100k tokens logged in the first 10 minutes, then a 50-minute silence.
    rows = [_row(start + timedelta(minutes=m), inp=50_000, out=0)
            for m in (1, 9)]
    now = start + timedelta(minutes=60)
    b = anchored_session(rows, resets, now=now)
    fc = forecast(b, now=now, cloud_5h_pct=0.50)

    # 100k tokens over the 60 minutes the window has been open.
    assert abs(fc.burn_rate_per_min_tokens - 100_000 / 60) < 1.0
    # 50% in 60 minutes -> another 60 minutes to 100%, on the same 60.
    assert abs(fc.eta_to_limit.total_seconds() / 60 - 60) < 0.5
    assert fc.eta_block_end == timedelta(hours=4)


def test_forecast_at_or_past_the_limit_is_immediate():
    resets = datetime(2026, 8, 20, 5, 30, tzinfo=timezone.utc)
    now = resets - timedelta(hours=1)
    b = anchored_session([_row(now - timedelta(minutes=5))], resets, now=now)
    assert forecast(b, now=now, cloud_5h_pct=1.0).eta_to_limit == timedelta(0)
    assert forecast(b, now=now, cloud_5h_pct=1.4).eta_to_limit == timedelta(0)


def test_forecast_of_no_block_is_all_zeroes():
    fc = forecast(None)
    assert (fc.burn_rate_per_min_tokens, fc.burn_rate_per_min_cost) == (0.0,
                                                                        0.0)
    assert fc.eta_block_end is None and fc.eta_to_limit is None


def test_elapsed_is_clamped_to_the_window_length():
    resets = datetime(2026, 8, 20, 5, 30, tzinfo=timezone.utc)
    b = anchored_session([], resets, now=resets - timedelta(minutes=1))
    assert b.elapsed(b.start - timedelta(hours=1)) == timedelta(0)
    assert b.elapsed(b.start + timedelta(hours=2)) == timedelta(hours=2)
    assert b.elapsed(b.end + timedelta(hours=3)) == timedelta(
        hours=BLOCK_HOURS)


def test_malformed_rows_are_skipped_not_fatal():
    base = datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc)
    rows = [
        _row(base),
        {'timestamp': None},
        {'timestamp': 'not-a-date'},
        {'timestamp': 12345},
        {},
        {'timestamp': (base + timedelta(minutes=5)).isoformat(),
         'input_tokens': None, 'cost_usd': None},
    ]
    b = compute_blocks(rows)
    assert len(b) == 1
    assert b[0].messages == 2
