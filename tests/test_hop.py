import json
from datetime import datetime, timedelta, timezone

from cct import hop


def _profile(d, name, token, email, plan):
    (d / f'{name}.json').write_text(json.dumps(
        {'email': email, 'plan': plan, 'claudeAiOauth': {'accessToken': token}}))


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_ACCOUNTS_DIR', str(tmp_path))
    _profile(tmp_path, 'alpha', 'tok-A', 'a@example.com', 'team')
    _profile(tmp_path, 'beta', 'tok-B', 'b@example.com', 'max')
    (tmp_path / 'active').write_text('alpha\n')
    return tmp_path


def test_not_in_use_when_there_is_nothing_saved(tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_ACCOUNTS_DIR', str(tmp_path))
    assert hop.load_state() is None


def test_missing_directory_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_ACCOUNTS_DIR', str(tmp_path / 'nope'))
    assert hop.load_state() is None


def test_live_account_comes_from_the_token_then_the_pointer(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert hop.load_state('tok-A').active == 'alpha'
    assert hop.load_state('tok-B').active == 'beta'          # token beats pointer
    assert hop.load_state('tok-rotated').active == 'alpha'   # falls back to pointer


def test_profiles_carry_email_and_plan_but_no_token(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    st = hop.load_state()
    assert [(p.name, p.email, p.plan) for p in st.profiles] == [
        ('alpha', 'a@example.com', 'team'), ('beta', 'b@example.com', 'max')]
    assert not any('tok-' in repr(p) for p in st.profiles)


def test_backup_files_are_not_accounts(tmp_path, monkeypatch):
    d = _setup(tmp_path, monkeypatch)
    (d / 'alpha.json.bak').write_text('{}')
    (d / '.tmp-claudehop').write_text('{}')
    assert [p.name for p in hop.load_state().profiles] == ['alpha', 'beta']


def test_provider_on_and_off(tmp_path, monkeypatch):
    d = _setup(tmp_path, monkeypatch)
    (d / 'bedrock.provider').write_text('claude-bedrock\n')
    assert hop.load_state().provider is None
    (d / 'provider').write_text('bedrock\n')
    st = hop.load_state('tok-A')
    assert (st.provider, st.provider_command, st.active) == (
        'bedrock', 'claude-bedrock', 'alpha')
    (d / 'bedrock.provider').unlink()      # a pointer to a deleted provider is off
    assert hop.load_state().provider is None


def _iso(dt):
    return dt.isoformat()


def test_usage_text_live_read_reset_and_never():
    now = datetime.now(timezone.utc)
    live = hop.Reading(40, _iso(now + timedelta(hours=2)), 62,
                       _iso(now + timedelta(days=3)), None, now, 'live')
    assert hop.usage_text(live, now) == ('5-hour 40% · 7-day 62%', 'live')
    seen = hop.reading_from_seen({
        'pct5': 34, 'pct7': 61, 'resets_5h': _iso(now + timedelta(hours=2)),
        'resets_7d': _iso(now + timedelta(days=3)), 'week_scope': None,
        'seen_at': _iso(now - timedelta(minutes=5))})
    assert hop.usage_text(seen, now) == ('5-hour 34% · 7-day 61%', 'read 5m ago')
    old = hop.reading_from_seen({
        'pct5': 97, 'pct7': 61, 'resets_5h': _iso(now - timedelta(hours=1)),
        'resets_7d': _iso(now + timedelta(days=3)),
        'seen_at': _iso(now - timedelta(hours=3))})
    assert hop.usage_text(old, now) == (
        '5-hour reset, was 97% · 7-day 61%', 'read 3h00m ago')
    assert hop.usage_text(None, now) == ('no reading yet', '')


def _cache(d, doc):
    (d / '.usage-cache').write_text(json.dumps(doc))


def test_team_seat_cache_entry_shows_its_model_meter(tmp_path, monkeypatch):
    d = _setup(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    _cache(d, {'alpha': {
        'windows': {'five_hour': {'pct': 84.0, 'resetsAt': _iso(now + timedelta(hours=1))},
                    'seven_day': None},
        'limits': [{'kind': 'session', 'group': 'session', 'pct': 84.0,
                    'resetsAt': None, 'scope': None},
                   {'kind': 'weekly_scoped', 'group': 'weekly', 'pct': 9.0,
                    'resetsAt': _iso(now + timedelta(days=4)), 'scope': 'Fable'}],
        'fetchedAt': (now - timedelta(minutes=10)).timestamp(), 'source': 'usage'}})
    r = hop.load_usage_cache()['alpha']
    assert (r.pct5, r.pct7, r.week_scope, r.source) == (84.0, 9.0, 'Fable', 'claudehop')
    assert hop.usage_text(r, now) == ('5-hour 84% · 7-day Fable 9%', 'read 10m ago')


def test_probe_and_throttle_are_named(tmp_path, monkeypatch):
    d = _setup(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    _cache(d, {
        'alpha': {'windows': {'five_hour': {'pct': 5.0, 'resetsAt': None}},
                  'fetchedAt': now.timestamp() - 120, 'source': 'probe',
                  'throttledUntil': now.timestamp() + 1500},
        'beta': {'windows': None, 'throttledUntil': now.timestamp() + 600},
    })
    cache = hop.load_usage_cache()
    usage, fresh = hop.usage_text(cache['alpha'], now)
    assert usage == '5-hour 5% · 7-day ?'
    assert fresh.startswith('read 2m ago through claude; server throttling reads for 2')
    assert hop.usage_text(cache['beta'], now)[0] == 'no reading yet'
    assert 'throttling' in hop.usage_text(cache['beta'], now)[1]


def test_the_newer_reading_wins_and_keeps_the_throttle():
    now = datetime.now(timezone.utc)
    a = hop.Reading(10, None, 5, None, None, now - timedelta(hours=2), 'widget')
    b = hop.Reading(50, None, 9, None, 'Fable', now - timedelta(minutes=1), 'claudehop',
                    now + timedelta(minutes=20))
    assert hop.newest(a, b) == b
    c = hop.Reading(70, None, 9, None, None, now, 'widget')
    best = hop.newest(c, b)
    assert best.pct5 == 70 and best.throttled_until == b.throttled_until
    assert hop.newest(None, None) is None


def test_a_missing_week_comes_from_the_older_reading():
    now = datetime.now(timezone.utc)
    old = hop.Reading(10, None, 9, 'w', 'Fable', now - timedelta(hours=1), 'claudehop')
    new = hop.Reading(50, None, None, None, None, now, 'widget')
    best = hop.newest(new, old)
    assert (best.pct5, best.pct7, best.week_scope, best.at) == (50, 9, 'Fable', now)


def test_live_login_uses_the_live_reading(tmp_path, monkeypatch):
    d = _setup(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    _cache(d, {'alpha': {'windows': {'five_hour': {'pct': 1.0, 'resetsAt': None}},
                         'fetchedAt': now.timestamp()}})
    live = hop.Reading(40, None, 3, None, None, now, 'live')
    got = hop.all_readings(['alpha', 'beta'], {}, 'alpha', live)
    assert got['alpha'] is live and got['beta'] is None


def test_most_room_follows_hop_next(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)

    def r(p5, mins=None, p7=None, scope=None, wk_reset=None):
        return hop.Reading(p5, _iso(now + timedelta(minutes=mins)) if mins else None,
                           p7, wk_reset, scope, now, 'claudehop')
    readings = {
        'alpha': r(10),                      # live login: never picked
        'a': r(97, 30),                      # full
        'b': r(12, 200),
        'c': r(15, 40),                      # same ten points as b, resets sooner
        'd': r(0, None, 100, None),          # all-model week used up
        'e': r(0, None, 100, 'Fable'),       # a one-model meter does not stop it
    }
    names = list(readings)
    pick = hop.most_room(readings, names, 'alpha', now)
    assert pick.name == 'e' and not pick.listed
    readings['e'] = r(25, 10)
    assert hop.most_room(readings, names, 'alpha', now).name == 'c'
    # With a `next` list, only those logins are candidates.
    (hop.accounts_dir() / 'next').write_text('a\nb\n# comment\n')
    pick = hop.most_room(readings, names, 'alpha', now)
    assert (pick.name, pick.listed) == ('b', True)
    readings['b'] = r(99, 90)
    pick = hop.most_room(readings, names, 'alpha', now)
    assert pick.name is None and pick.why.startswith('no login has room; a frees up first')


def test_run_terminals_and_sessions(tmp_path, monkeypatch):
    import os
    d = _setup(tmp_path, monkeypatch)
    home = d / '.run' / 'beta'
    (home / '.hop-pids').mkdir(parents=True)
    me = os.getpid()
    (home / '.hop-pids' / str(me)).write_text(hop._proc_start(me))
    (home / '.hop-pids' / '999999999').write_text('1')          # gone
    (home / 'sessions').mkdir()
    started = datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc)
    (home / 'sessions' / f'{me}.json').write_text(json.dumps({
        'pid': me, 'sessionId': 'sess-1', 'procStart': hop._proc_start(me),
        'startedAt': int(started.timestamp() * 1000)}))
    (home / 'sessions' / '999999999.json').write_text(json.dumps({
        'pid': 999999999, 'sessionId': 'sess-dead', 'startedAt': 1}))
    assert hop.run_terminals() == {'beta': 1}
    got = hop.run_sessions()
    assert [(s.session_id, s.account, s.since) for s in got] == [('sess-1', 'beta', started)]


def _fake_hop(tmp_path, body):
    script = tmp_path / 'fake-claudehop'
    script.write_text('#!/bin/sh\n' + body)
    script.chmod(0o755)
    return str(script)


def test_read_all_reports_a_missing_claudehop(monkeypatch):
    monkeypatch.setattr(hop, 'find_claudehop', lambda: None)
    assert hop.read_all().status == 'error'


def test_read_all_collects_notes(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    exe = _fake_hop(tmp_path, 'echo \'{"accounts": [{"name": "a", "note": null}, '
                              '{"name": "b", "note": "the server rejected this token"}]}\'\n')
    monkeypatch.setattr(hop, 'find_claudehop', lambda: exe)
    got = hop.read_all()
    assert (got.status, got.notes) == ('done', {'a': '', 'b': 'the server rejected this token'})


def test_read_all_does_not_start_while_the_lock_is_held(tmp_path, monkeypatch):
    import fcntl, os
    d = _setup(tmp_path, monkeypatch)
    marker = tmp_path / 'ran'
    monkeypatch.setattr(hop, 'find_claudehop', lambda: _fake_hop(tmp_path, f'touch {marker}\n'))
    fd = os.open(d / '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)                 # an open `hop` picker
    try:
        got = hop.read_all()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
    assert got.status == 'busy' and not marker.exists()


def test_a_queued_or_slow_read_is_left_running(tmp_path, monkeypatch):
    import time
    _setup(tmp_path, monkeypatch)
    done = tmp_path / 'finished'
    monkeypatch.setattr(hop, 'find_claudehop', lambda: _fake_hop(
        tmp_path, f'echo "waiting for another claudehop to finish..." >&2\n'
                  f'sleep 1\necho \'{{"accounts": []}}\'\ntouch {done}\n'))
    assert hop.read_all(wait=5).status == 'busy'
    assert hop.read_all().status == 'slow'         # never two at once
    for _ in range(50):                            # it finishes on its own, not killed
        if done.exists():
            break
        time.sleep(0.1)
    assert done.exists()
    monkeypatch.setattr(hop, 'find_claudehop', lambda: _fake_hop(
        tmp_path, 'sleep 1\necho \'{"accounts": []}\'\n'))
    for _ in range(50):
        if hop._running.poll() is not None:
            break
        time.sleep(0.1)
    assert hop.read_all(wait=0.2).status == 'slow'
