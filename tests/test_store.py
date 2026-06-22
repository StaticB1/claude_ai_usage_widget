from datetime import datetime, timedelta, timezone

import pytest

from cct.parser import Turn
from cct.pricing import DEFAULT_RATE_CARD, RateCard
from cct.store import Store


def _turn(ts, project='proj', model='claude-sonnet-4-7',
          msg_id=None, inp=100, out=200, cc_5m=0, cc_1h=0, cr=0,
          tool_uses=None, sidechain=False):
    return Turn(
        timestamp=ts, project=project,
        msg_id=msg_id, request_id=None, uuid=None,
        session_id='s-1', model=model,
        input_tokens=inp, cache_creation_5m=cc_5m, cache_creation_1h=cc_1h,
        cache_read=cr, output_tokens=out, is_sidechain=sidechain,
        tool_uses=tool_uses or {},
    )


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'h.db')


@pytest.fixture
def rc():
    return RateCard(DEFAULT_RATE_CARD)


def test_upsert_and_dedup(store, rc):
    ts = datetime(2026, 4, 28, 10, tzinfo=timezone.utc)
    t = _turn(ts, msg_id='m1')
    assert store.upsert_turns([t], rc) == 1
    # Second insert with same msg_id → ignored
    assert store.upsert_turns([t], rc) == 0
    rows = store.query()
    assert len(rows) == 1
    assert rows[0]['cost_usd'] > 0


def test_project_summary(store, rc):
    ts = datetime(2026, 4, 28, 10, tzinfo=timezone.utc)
    store.upsert_turns([
        _turn(ts, project='a', msg_id='m1', inp=100, out=200),
        _turn(ts + timedelta(minutes=1), project='a', msg_id='m2',
              inp=50, out=100),
        _turn(ts + timedelta(minutes=2), project='b', msg_id='m3',
              inp=10, out=10),
    ], rc)
    summary = store.project_summary()
    by_name = {s['project']: s for s in summary}
    assert by_name['a']['messages'] == 2
    assert by_name['a']['total_tokens'] == 100 + 200 + 50 + 100
    assert by_name['b']['messages'] == 1


def test_period_filter(store, rc):
    now = datetime.now(timezone.utc)
    store.upsert_turns([
        _turn(now - timedelta(days=10), msg_id='old'),
        _turn(now, msg_id='new'),
    ], rc)
    rows = store.query(since=now - timedelta(days=1))
    assert len(rows) == 1


def test_tool_summary_aggregates(store, rc):
    ts = datetime(2026, 4, 28, 10, tzinfo=timezone.utc)
    store.upsert_turns([
        _turn(ts, msg_id='m1',
              tool_uses={'Bash': 2, 'Read': 1}),
        _turn(ts + timedelta(minutes=1), msg_id='m2',
              tool_uses={'Bash': 1}),
    ], rc)
    out = {t['name']: t for t in store.tool_summary()}
    assert out['Bash']['calls'] == 3
    assert out['Bash']['messages'] == 2
    assert out['Read']['calls'] == 1


def test_reprice_after_rate_change(store, rc):
    ts = datetime(2026, 4, 28, 10, tzinfo=timezone.utc)
    store.upsert_turns([_turn(ts, msg_id='m1', inp=1_000_000, out=0)], rc)
    initial = store.total_cost()
    # Override Sonnet input price 3x
    new_table = dict(DEFAULT_RATE_CARD)
    new_table['claude-sonnet-4-7'] = (9.0, 9.0, 18.0, 0.9, 45.0)
    rc2 = RateCard(new_table)
    store.reprice_all(rc2)
    assert store.total_cost() == pytest.approx(initial * 3)


def test_budget_crud(store):
    bid = store.add_budget(
        'cap', 'global', 'month',
        limit_usd=100.0, limit_tokens=None, notify_at_pct=80,
    )
    assert bid > 0
    assert any(b['id'] == bid for b in store.list_budgets())
    store.delete_budget(bid)
    assert not any(b['id'] == bid for b in store.list_budgets())


def test_budget_requires_limit(store):
    with pytest.raises(ValueError):
        store.add_budget('x', 'global', 'month', None, None)


def test_budget_rejects_bad_period(store):
    with pytest.raises(ValueError):
        store.add_budget('x', 'global', 'fortnight', limit_usd=10.0,
                         limit_tokens=None)
