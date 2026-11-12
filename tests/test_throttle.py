import json
import stat
from datetime import datetime, timedelta, timezone

from cct import throttle


def test_poll_interval_is_two_minutes_and_never_below_a_minute(tmp_path, monkeypatch):
    from cct import config
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')
    assert config.load_settings().poll_interval_seconds == 120          # default
    for asked, used in ((120, 120), (900, 900), (5, 60), (59, 60)):
        (tmp_path / 'config.json').write_text(json.dumps({'poll_interval_seconds': asked}))
        assert config.load_settings().poll_interval_seconds == used


def test_keys_follow_the_claudehop_login_when_known():
    assert throttle.key_for('Default', 'ebenworks-admin') == 'hop:ebenworks-admin'
    assert throttle.key_for('Default') == 'account:Default'


def test_a_refusal_survives_a_restart_and_lapses(tmp_path):
    path = tmp_path / 'throttle.json'
    now = datetime.now(timezone.utc)
    t = throttle.Throttles(path)
    assert t.until('hop:a', now) is None
    t.set('hop:a', now + timedelta(minutes=30))
    t.set('hop:b', now + timedelta(minutes=5))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    again = throttle.Throttles(path)             # the widget restarted
    assert again.until('hop:a', now) == now + timedelta(minutes=30)
    assert again.until('hop:b', now + timedelta(minutes=6)) is None   # lapsed
    again.clear('hop:a')
    assert 'hop:a' not in json.loads(path.read_text())


def test_a_damaged_file_counts_as_no_refusals(tmp_path):
    path = tmp_path / 'throttle.json'
    path.write_text('{not json')
    assert throttle.Throttles(path).until('hop:a') is None
    path.write_text(json.dumps({'hop:a': 'yesterday', 'hop:b': 5}))
    assert throttle.Throttles(path).until('hop:a') is None


def test_the_request_log_names_outcomes_and_stays_short(tmp_path, monkeypatch):
    log = tmp_path / 'usage-requests.log'
    at = datetime(2026, 11, 11, 2, 30, tzinfo=timezone.utc)
    throttle.log_request('hop:a', 200, path=log, now=at)
    throttle.log_request('hop:a', 429, 1860, path=log, now=at)
    lines = log.read_text().splitlines()
    assert lines[0].endswith(' hop:a 200')
    assert lines[1].endswith(' hop:a 429 retry-after=1860')
    assert stat.S_IMODE(log.stat().st_mode) == 0o600

    monkeypatch.setattr(throttle, 'REQUEST_LOG_MAX_BYTES', 400)
    for i in range(30):
        throttle.log_request(f'hop:n{i}', 200, path=log, now=at)
    text = log.read_text()
    assert len(text) <= 400
    assert text.endswith(' hop:n29 200\n')
    assert all(ln.startswith('2026-11-11T') for ln in text.splitlines())   # whole lines only


def test_a_backoff_holds_only_for_the_token_that_earned_it():
    now = datetime.now(timezone.utc)
    later = now + timedelta(minutes=40)
    # the same login, still refused: the poll waits
    assert throttle.backoff_holds('rate_limited', later, now, 'tok-old', 'tok-old')
    # a hop put another account's token in the file: asked now, not in 40 minutes
    assert not throttle.backoff_holds('rate_limited', later, now, 'tok-old', 'tok-new')
    # a refusal that has run out, or a state that is not one
    assert not throttle.backoff_holds('rate_limited', now - timedelta(seconds=1), now, 't', 't')
    assert not throttle.backoff_holds('rate_limited', None, now, 't', 't')
    assert not throttle.backoff_holds('ok', later, now, 't', 't')


def test_a_renewed_token_of_the_same_account_is_still_refused(tmp_path):
    # backoff_holds lets a renewed token through; the per-account record the
    # poll reads next is what still holds it back.
    now = datetime.now(timezone.utc)
    t = throttle.Throttles(tmp_path / 'throttle.json')
    t.set(throttle.key_for('Default', 'alpha'), now + timedelta(minutes=40))
    assert not throttle.backoff_holds('rate_limited', now + timedelta(minutes=40), now, 'a1', 'a2')
    assert t.until(throttle.key_for('Default', 'alpha'), now) is not None
    assert t.until(throttle.key_for('Default', 'beta'), now) is None
