from datetime import datetime, timedelta, timezone

import pytest

from cct.budgets import evaluate_budgets, period_window
from cct.parser import Turn
from cct.pricing import DEFAULT_RATE_CARD, RateCard
from cct.store import Store


def _turn(ts, **kw):
    defaults = dict(
        timestamp=ts, project='proj',
        msg_id=None, request_id=None, uuid=None,
        session_id='s-1', model='claude-sonnet-4-7',
        input_tokens=1_000_000, cache_creation_5m=0, cache_creation_1h=0,
        cache_read=0, output_tokens=0, is_sidechain=False,
        tool_uses={},
    )
    defaults.update(kw)
    return Turn(**defaults)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'h.db')


@pytest.fixture
def rc():
    return RateCard(DEFAULT_RATE_CARD)


def test_period_window_day():
    now = datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc)
    start, end, key = period_window('day', now)
    assert start == datetime(2026, 4, 28, tzinfo=timezone.utc)
    assert end == datetime(2026, 4, 29, tzinfo=timezone.utc)
    assert key == '2026-04-28'


def test_period_window_month():
    now = datetime(2026, 4, 28, tzinfo=timezone.utc)
    start, end, key = period_window('month', now)
    assert start == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert key == '2026-04'


def test_period_window_month_year_rollover():
    now = datetime(2026, 12, 31, tzinfo=timezone.utc)
    _, end, _ = period_window('month', now)
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_global_budget_evaluates_spend(store, rc):
    now = datetime.now(timezone.utc)
    # 1M Sonnet input = $3
    store.upsert_turns([_turn(now, msg_id='m1')], rc)
    bid = store.add_budget('cap', 'global', 'month',
                           limit_usd=10.0, limit_tokens=None)
    states = evaluate_budgets(store, now)
    s = next(x for x in states if x.id == bid)
    assert s.spent_usd == pytest.approx(3.0)
    assert 0.29 < s.pct < 0.31  # ~30%


def test_project_scoped_budget(store, rc):
    now = datetime.now(timezone.utc)
    store.upsert_turns([
        _turn(now, msg_id='m1', project='alpha'),
        _turn(now, msg_id='m2', project='beta'),
    ], rc)
    bid = store.add_budget('alpha-cap', 'project:alpha', 'month',
                           limit_usd=10.0, limit_tokens=None)
    s = next(x for x in evaluate_budgets(store, now) if x.id == bid)
    assert s.spent_usd == pytest.approx(3.0)  # only alpha


def test_token_budget(store, rc):
    now = datetime.now(timezone.utc)
    store.upsert_turns([_turn(now, msg_id='m1')], rc)
    bid = store.add_budget('tok', 'global', 'month',
                           limit_usd=None, limit_tokens=2_000_000)
    s = next(x for x in evaluate_budgets(store, now) if x.id == bid)
    assert s.spent_tokens == 1_000_000
    assert s.pct == pytest.approx(0.5)


def test_should_notify_logic(store, rc):
    now = datetime.now(timezone.utc)
    store.upsert_turns([_turn(now, msg_id='m1')], rc)  # 50% of $6
    bid = store.add_budget('cap', 'global', 'month',
                           limit_usd=6.0, limit_tokens=None,
                           notify_at_pct=50)
    s = next(x for x in evaluate_budgets(store, now) if x.id == bid)
    assert s.should_notify()
    # After notification recorded for this period, same pct → silent
    store.update_budget_notification(bid, int(s.pct * 100), s.period_key)
    s2 = next(x for x in evaluate_budgets(store, now) if x.id == bid)
    assert not s2.should_notify()


# ── Plan-utilization budgets (Max / Pro / Team) ───────────────────────────

def test_pct_budget_requires_rolling_period(store):
    with pytest.raises(ValueError):
        store.add_budget('p', 'global', 'month',
                         limit_usd=None, limit_tokens=None, limit_pct=80.0)


def test_pct_budget_requires_global_scope(store):
    with pytest.raises(ValueError):
        store.add_budget('p', 'project:alpha', '5h',
                         limit_usd=None, limit_tokens=None, limit_pct=80.0)


def test_pct_budget_rejects_out_of_range(store):
    with pytest.raises(ValueError):
        store.add_budget('p', 'global', '5h',
                         limit_usd=None, limit_tokens=None, limit_pct=0)
    with pytest.raises(ValueError):
        store.add_budget('p', 'global', '5h',
                         limit_usd=None, limit_tokens=None, limit_pct=101)


def test_pct_budget_evaluates_from_cloud_data(store):
    bid = store.add_budget('5h-cap', 'global', '5h',
                           limit_usd=None, limit_tokens=None, limit_pct=80.0)
    usage = {'five_hour': {'utilization': 60,  # 60%
                           'resets_at': '2026-04-28T18:00:00Z'},
             'seven_day': {'utilization': 20}}
    s = next(x for x in evaluate_budgets(store, usage_data=usage)
             if x.id == bid)
    assert s.is_pct_based
    assert s.data_available
    assert s.spent_pct == pytest.approx(60.0)
    assert s.pct == pytest.approx(0.75)  # 60 / 80


def test_pct_budget_low_percent_not_misscaled(store):
    # Regression for issue #4: the usage API reports utilization as a
    # percentage, so 1.0 means 1% — it must NOT be rescaled to 100%.
    bid = store.add_budget('5h-cap', 'global', '5h',
                           limit_usd=None, limit_tokens=None, limit_pct=80.0)
    usage = {'five_hour': {'utilization': 1.0}}
    s = next(x for x in evaluate_budgets(store, usage_data=usage)
             if x.id == bid)
    assert s.spent_pct == pytest.approx(1.0)   # 1%, not 100%
    assert s.pct == pytest.approx(0.0125)      # 1 / 80


def test_pct_budget_without_cloud_data_is_dormant(store):
    bid = store.add_budget('7d-cap', 'global', '7d',
                           limit_usd=None, limit_tokens=None, limit_pct=90.0)
    s = next(x for x in evaluate_budgets(store, usage_data=None)
             if x.id == bid)
    assert s.is_pct_based
    assert not s.data_available
    assert not s.should_notify()


def test_pct_budget_should_notify_when_threshold_crossed(store):
    bid = store.add_budget('5h-cap', 'global', '5h',
                           limit_usd=None, limit_tokens=None,
                           limit_pct=80.0, notify_at_pct=70)
    # 60/80 = 75% of budget → above 70% notify threshold
    usage = {'five_hour': {'utilization': 60}}
    s = next(x for x in evaluate_budgets(store, usage_data=usage)
             if x.id == bid)
    assert s.should_notify()


def test_pct_period_key_changes_when_resets_at_advances(store):
    bid = store.add_budget('5h-cap', 'global', '5h',
                           limit_usd=None, limit_tokens=None, limit_pct=80.0)
    u1 = {'five_hour': {'utilization': 50,
                        'resets_at': '2026-04-28T18:00:00Z'}}
    u2 = {'five_hour': {'utilization': 50,
                        'resets_at': '2026-04-28T23:00:00Z'}}
    k1 = next(x for x in evaluate_budgets(store, usage_data=u1)
              if x.id == bid).period_key
    k2 = next(x for x in evaluate_budgets(store, usage_data=u2)
              if x.id == bid).period_key
    assert k1 != k2
