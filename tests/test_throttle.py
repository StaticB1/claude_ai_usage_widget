import json
import stat
from datetime import datetime, timedelta, timezone

from cct import throttle


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
