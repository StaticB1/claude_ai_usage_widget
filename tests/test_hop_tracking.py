"""Filing each turn under the claudehop account that made it."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from cct.parser import Turn
from cct.pricing import load_rate_card
from cct.store import Store

T0 = datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc)


def _turn(msg_id, at, session='s1'):
    return Turn(timestamp=at, project='p', msg_id=msg_id, request_id=None,
                uuid=None, session_id=session, model='claude-sonnet-4-6',
                input_tokens=10, cache_creation_5m=0, cache_creation_1h=0,
                cache_read=0, output_tokens=5, is_sidechain=False)


def _m(minutes):
    return T0 + timedelta(minutes=minutes)


def test_first_check_dates_the_live_account_from_its_hop(tmp_path):
    st = Store(tmp_path / 'h.db')
    st.track_hop('alpha', changed_at=_m(0), now=_m(30))
    who = st.hop_resolver()
    assert who(_turn('a', _m(-5))) is None          # before tracking: unknown
    assert who(_turn('b', _m(10))) == 'alpha'
    assert st.hop_tracking_since() == _m(0)


def test_a_hop_while_watching_is_dated_by_the_pointer(tmp_path):
    st = Store(tmp_path / 'h.db')
    st.track_hop('alpha', _m(0), now=_m(1))
    st.track_hop('alpha', _m(0), now=_m(1.2))
    # Hopped at minute 1.3, noticed at the next check.
    st.track_hop('beta', _m(1.3), now=_m(1.4))
    who = st.hop_resolver()
    assert who(_turn('a', _m(1.25))) == 'alpha'
    assert who(_turn('b', _m(1.35))) == 'beta'


def test_hops_nobody_saw_leave_a_gap(tmp_path):
    st = Store(tmp_path / 'h.db')
    st.track_hop('alpha', _m(0), now=_m(1))
    # Widget off for an hour; meanwhile a hop to gamma and back to beta.
    st.track_hop('beta', _m(50), now=_m(61))
    who = st.hop_resolver()
    assert who(_turn('a', _m(0.5))) == 'alpha'
    assert who(_turn('b', _m(30))) is None          # can't know
    assert who(_turn('c', _m(55))) == 'beta'


def test_quiet_time_without_a_hop_keeps_the_account(tmp_path):
    st = Store(tmp_path / 'h.db')
    st.track_hop('alpha', _m(0), now=_m(1))
    st.track_hop('alpha', _m(0), now=_m(120))       # off for two hours, no hop
    assert st.hop_resolver()(_turn('a', _m(60))) == 'alpha'


def test_hop_run_sessions_keep_their_own_account(tmp_path):
    st = Store(tmp_path / 'h.db')
    st.track_hop('alpha', _m(0), now=_m(1))
    run = SimpleNamespace(session_id='s-run', account='beta', since=_m(5))
    st.note_run_sessions([run], now=_m(6))
    st.note_run_sessions([run], now=_m(20))
    who = st.hop_resolver()
    assert who(_turn('a', _m(10), 's-run')) == 'beta'
    assert who(_turn('b', _m(10), 's-other')) == 'alpha'
    assert who(_turn('c', _m(4), 's-run')) == 'alpha'   # resumed: earlier turns were live
    assert who(_turn('d', _m(20.5), 's-run')) == 'beta'  # grace after last sighting
    assert who(_turn('e', _m(30), 's-run')) == 'alpha'


def test_turns_are_stored_and_filtered_by_hop_account(tmp_path):
    st = Store(tmp_path / 'h.db')
    for minute in (1, 3, 5, 7, 9):                  # checked every two minutes
        st.track_hop('alpha', _m(0), now=_m(minute))
    st.track_hop('beta', _m(10), now=_m(10.1))
    turns = [_turn('msg_01a', _m(5)), _turn('msg_01b', _m(15)),
             _turn('msg_01c', _m(-10)), _turn('msg_bdrk_1', _m(16))]
    st.upsert_turns(turns, load_rate_card(), account='default',
                    hop_of=st.hop_resolver())
    assert st.totals(account='hop:alpha')['messages'] == 1
    assert st.totals(account='hop:beta')['messages'] == 1
    assert st.totals(account='default')['messages'] == 3
    assert st.totals(account='bedrock')['messages'] == 1
    assert st.hop_accounts_with_rows() == ['alpha', 'beta']
    rows = {r['hop_account']: r['messages'] for r in st.hop_account_summary()}
    assert rows == {'alpha': 1, 'beta': 1, None: 1}     # Bedrock is not a login
    assert st.message_count(account='hop:alpha') == 1


def test_old_team_week_zero_is_forgotten(tmp_path):
    import sqlite3
    db = tmp_path / 'h.db'
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE hop_usage (name TEXT PRIMARY KEY, pct5 INTEGER,
                 pct7 INTEGER, resets_5h TEXT, resets_7d TEXT, seen_at TEXT NOT NULL)""")
    c.execute("INSERT INTO hop_usage VALUES ('team', 50, 0, 'r', NULL, 'now')")
    c.execute("INSERT INTO hop_usage VALUES ('max', 50, 12, 'r', 'w', 'now')")
    c.commit(); c.close()
    seen = Store(db).hop_usage()
    assert seen['team']['pct7'] is None and seen['max']['pct7'] == 12


def test_rows_stored_before_attribution_are_filed_again(tmp_path):
    st = Store(tmp_path / 'h.db')
    rc = load_rate_card()
    # An earlier version stored these with no claudehop account.
    st.upsert_turns([_turn('msg_01a', _m(5)), _turn('msg_01b', _m(-30))], rc,
                    account='default')
    since = st.track_hop('alpha', _m(0), now=_m(10))
    assert since == _m(0)
    assert st.reattribute('default', since) == 1
    assert st.totals(account='hop:alpha')['messages'] == 1   # the earlier row stays unknown


def test_a_hop_noticed_late_moves_the_turns_after_it(tmp_path):
    st = Store(tmp_path / 'h.db')
    rc = load_rate_card()
    st.track_hop('alpha', _m(0), now=_m(1))
    # Scanned before the hop at minute 1.5 was noticed: filed under alpha.
    st.upsert_turns([_turn('msg_01a', _m(1.4)), _turn('msg_01b', _m(1.6))], rc,
                    account='default', hop_of=st.hop_resolver())
    assert st.totals(account='hop:alpha')['messages'] == 2
    since = st.track_hop('beta', _m(1.5), now=_m(1.7))
    st.reattribute('default', since)
    assert st.totals(account='hop:alpha')['messages'] == 1
    assert st.totals(account='hop:beta')['messages'] == 1


def test_a_run_session_seen_late_takes_its_turns_back(tmp_path):
    st = Store(tmp_path / 'h.db')
    rc = load_rate_card()
    st.track_hop('alpha', _m(0), now=_m(1))
    st.upsert_turns([_turn('msg_01a', _m(5), 's-run')], rc, account='default',
                    hop_of=st.hop_resolver())
    run = SimpleNamespace(session_id='s-run', account='beta', since=_m(4))
    since = st.note_run_sessions([run], now=_m(6))
    assert since == _m(4)
    assert st.note_run_sessions([run], now=_m(7)) is None      # already known
    st.reattribute('default', since)
    assert st.totals(account='hop:beta')['messages'] == 1


def test_scan_backfills_once(tmp_path, monkeypatch):
    from cct import cli, hop
    from cct.config import Account
    home = tmp_path / 'claude'
    (home / 'accounts').mkdir(parents=True)
    (home / 'accounts' / 'alpha.json').write_text('{"email": "a@x"}')
    (home / 'accounts' / 'active').write_text('alpha\n')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home))
    monkeypatch.delenv('CLAUDE_ACCOUNTS_DIR', raising=False)
    st = Store(tmp_path / 'h.db')
    acc = Account('default', home)
    now = datetime.now(timezone.utc)
    st.upsert_turns([_turn('msg_01a', now - timedelta(seconds=5))], load_rate_card(),
                    account='default')
    import os
    os.utime(home / 'accounts' / 'active', (now.timestamp() - 60, now.timestamp() - 60))
    assert cli.track_hop(st, [acc]) == 'default'
    assert st.totals(account='hop:alpha')['messages'] == 1
    assert st.meta_get('hop_backfilled') == '1'
    assert hop.active_changed_at() is not None
