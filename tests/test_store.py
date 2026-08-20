import json
from datetime import datetime, timedelta, timezone

import pytest

from cct.cli import scan_into_store
from cct.parser import Turn
from cct.pricing import DEFAULT_RATE_CARD, RateCard
from cct.store import Store


def _assistant_line(msg_id, inp=100, out=50):
    return json.dumps({
        "type": "assistant",
        "timestamp": "2026-04-28T10:00:00Z",
        "sessionId": "s1",
        "cwd": "/home/u/proj",
        "requestId": msg_id,
        "message": {
            "id": msg_id,
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": inp, "output_tokens": out},
        },
    })


def _turn(ts, project='proj', model='claude-sonnet-4-6',
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


def test_scan_into_store_is_incremental(tmp_path, rc):
    proj = tmp_path / '-home-u-proj'
    proj.mkdir()
    f = proj / 'session.jsonl'
    f.write_text(_assistant_line('m1') + "\n")
    store = Store(tmp_path / 'h.db')

    # First scan imports the one turn and records the file signature.
    assert scan_into_store(store, rc, claude_dir=tmp_path) == {'default': 1}
    # Re-scan with the file unchanged: nothing is re-parsed at all.
    assert scan_into_store(store, rc, claude_dir=tmp_path) == {'default': 0}

    # Appending a turn changes the file size → it's re-read, and both turns
    # in it are written (the pre-existing one as a no-op refresh).
    with open(f, 'a') as fh:
        fh.write(_assistant_line('m2') + "\n")
    assert scan_into_store(store, rc, claude_dir=tmp_path) == {'default': 2}
    assert len(store.query()) == 2


def test_upsert_dedups_on_key(store, rc):
    ts = datetime(2026, 4, 28, 10, tzinfo=timezone.utc)
    t = _turn(ts, msg_id='m1')
    store.upsert_turns([t], rc)
    store.upsert_turns([t], rc)
    rows = store.query()
    assert len(rows) == 1
    assert rows[0]['cost_usd'] > 0


def test_upsert_refreshes_an_existing_row(store, rc):
    """A corrected re-parse must overwrite the stored row, not be ignored —
    that is what lets a parser or pricing fix repair history."""
    ts = datetime(2026, 4, 28, 10, tzinfo=timezone.utc)
    store.upsert_turns([_turn(ts, msg_id='m1', tool_uses={'Bash': 1})], rc)
    store.upsert_turns([_turn(ts, msg_id='m1', tool_uses={'Bash': 4})], rc)
    rows = store.query()
    assert len(rows) == 1
    assert json.loads(rows[0]['tool_uses_json']) == {'Bash': 4}


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
    new_table['claude-sonnet-4-6'] = (9.0, 11.25, 18.0, 0.9, 45.0)
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


def test_totals_matches_summing_rows(store, rc):
    """The SQL aggregate the dashboard now uses must agree with the row-by-row
    sum it replaced."""
    ts = datetime(2026, 4, 28, 10, tzinfo=timezone.utc)
    store.upsert_turns([
        _turn(ts, project='a', msg_id='m1', inp=100, out=200, cr=7),
        _turn(ts + timedelta(minutes=1), project='b', msg_id='m2',
              inp=50, out=100, cc_5m=3, cc_1h=5),
    ], rc)
    rows = store.query()
    by_hand = sum(r['input_tokens'] + r['cache_creation_5m']
                  + r['cache_creation_1h'] + r['cache_read']
                  + r['output_tokens'] for r in rows)
    t = store.totals()
    assert t['total_tokens'] == by_hand
    assert t['messages'] == 2
    assert t['projects'] == 2
    assert t['cost_usd'] == pytest.approx(sum(r['cost_usd'] for r in rows))


def test_daily_series_buckets_by_local_day(store, rc):
    """Buckets follow the user's calendar day, not UTC's."""
    from cct.periods import local_now
    now_local = local_now()
    store.upsert_turns([
        _turn(now_local.astimezone(timezone.utc), msg_id='m1'),
    ], rc)
    series = store.daily_series(days=3)
    assert series
    assert series[-1]['day'] == now_local.strftime('%Y-%m-%d')


def _turn_in(session, ts, project='proj', **kw):
    t = _turn(ts, project=project, **kw)
    t.session_id = session
    t.msg_id = f"{session}-{ts.isoformat()}"
    return t


def test_session_summary_groups_by_project_and_session(store, rc):
    base = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
    store.upsert_turns([
        _turn_in('s-a', base, inp=100, out=10),
        _turn_in('s-a', base + timedelta(minutes=30), inp=200, out=20),
        _turn_in('s-b', base + timedelta(hours=2), inp=1, out=1),
        _turn_in('s-c', base + timedelta(hours=3), project='other',
                 inp=5, out=5),
    ], rc)
    rows = {(r['project'], r['session_id']): r
            for r in store.session_summary()}
    assert set(rows) == {('proj', 's-a'), ('proj', 's-b'), ('other', 's-c')}
    a = rows[('proj', 's-a')]
    assert a['messages'] == 2
    assert a['input_tokens'] == 300
    assert a['output_tokens'] == 30
    assert a['first_used'] == base.isoformat()
    assert a['last_used'] == (base + timedelta(minutes=30)).isoformat()


def test_session_summary_is_newest_first(store, rc):
    base = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
    store.upsert_turns([
        _turn_in('old', base),
        _turn_in('new', base + timedelta(hours=4)),
    ], rc)
    assert [r['session_id'] for r in store.session_summary()] == ['new', 'old']


def test_session_summary_agrees_with_the_project_totals(store, rc):
    base = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
    store.upsert_turns([
        _turn_in(f's-{i}', base + timedelta(minutes=i), inp=i, out=i * 2)
        for i in range(1, 12)
    ], rc)
    sessions = store.session_summary()
    proj = store.project_summary()[0]
    assert sum(r['messages'] for r in sessions) == proj['messages']
    assert sum(r['total_tokens'] for r in sessions) == proj['total_tokens']
    assert abs(sum(r['cost_usd'] for r in sessions)
               - proj['cost_usd']) < 1e-9


def test_session_summary_honours_the_period_cutoff(store, rc):
    base = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
    store.upsert_turns([
        _turn_in('old', base - timedelta(days=10)),
        _turn_in('recent', base),
    ], rc)
    rows = store.session_summary(since=base - timedelta(days=1))
    assert [r['session_id'] for r in rows] == ['recent']
