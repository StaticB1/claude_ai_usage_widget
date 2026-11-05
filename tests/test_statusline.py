import io
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from cct import cli, config, statusline
from cct import hop as claudehop

RESET_5H = 1793900000
RESET_7D = 1794400000


def _payload(five=31.0, week=29.0):
    rl = {}
    if five is not None:
        rl['five_hour'] = {'used_percentage': five, 'resets_at': RESET_5H}
    if week is not None:
        rl['seven_day'] = {'used_percentage': week, 'resets_at': RESET_7D}
    return {'model': {'display_name': 'Opus'}, 'rate_limits': rl}


@pytest.fixture
def here(tmp_path, monkeypatch):
    monkeypatch.setattr(statusline, 'READINGS_DIR', tmp_path / 'readings')
    monkeypatch.setenv('CLAUDE_ACCOUNTS_DIR', str(tmp_path / 'none'))
    home = tmp_path / 'claude'
    home.mkdir()
    return home


def test_the_windows_are_kept_in_the_usage_api_shape(here):
    assert statusline.save(_payload(), here)
    entry = statusline.for_dir(here)
    cloud = statusline.as_cloud(entry)
    assert cloud['five_hour']['utilization'] == 31.0
    assert cloud['seven_day']['utilization'] == 29.0
    assert cloud['five_hour']['resets_at'] == datetime.fromtimestamp(
        RESET_5H, timezone.utc).isoformat()
    r = claudehop.reading_from_cloud(cloud, statusline.read_at(entry), 'statusline')
    assert (r.pct5, r.pct7, r.source) == (31.0, 29.0, 'statusline')


def test_a_payload_without_rate_limits_keeps_nothing(here):
    # Team seats, API keys and a session before its first reply have none.
    assert not statusline.save({'model': {'display_name': 'Opus'}}, here)
    assert not statusline.save(_payload(None, None), here)
    assert not statusline.save({'rate_limits': {'five_hour': {'used_percentage': True}}}, here)
    assert statusline.load_all() == []


def test_one_window_is_enough(here):
    assert statusline.save(_payload(week=None), here)
    cloud = statusline.as_cloud(statusline.for_dir(here))
    assert cloud['seven_day'] is None and cloud['five_hour']['utilization'] == 31.0


def test_each_home_keeps_its_newest_reading(here, tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    t0 = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)
    statusline.save(_payload(10), here, t0)
    statusline.save(_payload(20), here, t0 + timedelta(minutes=1))
    statusline.save(_payload(70), other, t0 + timedelta(minutes=2))
    assert statusline.for_dir(here)['five_hour']['used_percentage'] == 20
    assert statusline.for_dir(other)['five_hour']['used_percentage'] == 70
    assert statusline.load_all()[0]['config_dir'] == str(other.resolve())


def test_the_account_comes_from_a_hop_run_home_or_the_active_pointer(tmp_path, monkeypatch):
    monkeypatch.delenv('CLAUDE_ACCOUNTS_DIR', raising=False)
    home = tmp_path / 'claude'
    (home / 'accounts' / '.run' / 'work').mkdir(parents=True)
    assert statusline.hop_name_for(home) is None
    (home / 'accounts' / 'active').write_text('home\n')
    assert statusline.hop_name_for(home) == 'home'
    assert statusline.hop_name_for(home / 'accounts' / '.run' / 'work') == 'work'


def test_by_hop_name_keeps_each_accounts_newest(here, tmp_path, monkeypatch):
    t0 = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)
    acc = tmp_path / 'accounts'
    acc.mkdir()
    monkeypatch.setenv('CLAUDE_ACCOUNTS_DIR', str(acc))
    (acc / 'active').write_text('alpha\n')
    statusline.save(_payload(10), here, t0)
    (acc / 'active').write_text('beta\n')
    statusline.save(_payload(50), here, t0 + timedelta(minutes=5))
    run_home = acc / '.run' / 'alpha'
    run_home.mkdir(parents=True)
    statusline.save(_payload(12), run_home, t0 + timedelta(minutes=1))
    got = statusline.by_hop_name()
    assert got['beta']['five_hour']['used_percentage'] == 50
    assert got['alpha']['five_hour']['used_percentage'] == 12


def _run(monkeypatch, capsys, payload, command):
    raw = json.dumps(payload).encode()
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(raw)))
    code = statusline.run(command)
    return code, capsys.readouterr()


def test_run_saves_then_shows_the_existing_status_line(here, monkeypatch, capsys):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(here))
    code, out = _run(monkeypatch, capsys, _payload(), ['python3', '-c',
                     '"import sys,json; print(\'mine\', json.load(sys.stdin)[\'model\'][\'display_name\'])"'])
    assert code == 0
    assert out.out.strip() == 'mine Opus'
    assert statusline.for_dir(here)['five_hour']['used_percentage'] == 31.0


def test_run_without_a_command_prints_its_own_line(here, monkeypatch, capsys):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(here))
    code, out = _run(monkeypatch, capsys, _payload(), [])
    assert code == 0
    assert out.out.strip() == 'Opus · 5h 31% · 7d 29%'


def test_a_failed_save_never_breaks_the_status_line(here, monkeypatch, capsys):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(here))

    def boom(*a, **k):
        raise OSError('disk full')
    monkeypatch.setattr(statusline, 'save', boom)
    code, out = _run(monkeypatch, capsys, _payload(), [])
    assert code == 0 and '5h 31%' in out.out
    assert 'could not save' in out.err


def test_the_cli_reads_the_status_line_and_leaves_the_token_alone(here, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(here))
    statusline.save(_payload(44), here)
    calls = []
    monkeypatch.setattr(cli, 'fetch_cloud_usage', lambda tok: calls.append(tok) or {})
    monkeypatch.setattr(cli, 'load_token', lambda *a: 'tok')
    monkeypatch.setattr(cli, 'load_settings', lambda: config.Settings(token_polling=True))
    assert cli._safe_cloud_data()['five_hour']['utilization'] == 44
    assert calls == []


def test_the_cli_polls_only_when_polling_is_on(here, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(here))
    calls = []
    monkeypatch.setattr(cli, 'fetch_cloud_usage', lambda tok: calls.append(tok) or {'five_hour': {}})
    monkeypatch.setattr(cli, 'load_token', lambda *a: 'tok')
    monkeypatch.setattr(cli, 'load_settings', lambda: config.Settings())
    assert cli._safe_cloud_data() is None and calls == []
    monkeypatch.setattr(cli, 'load_settings', lambda: config.Settings(token_polling=True))
    assert cli._safe_cloud_data() == {'five_hour': {}} and calls == ['tok']


def test_a_reading_from_before_a_hop_is_not_the_new_logins(here, tmp_path, monkeypatch):
    acc = tmp_path / 'accounts'
    acc.mkdir()
    monkeypatch.setenv('CLAUDE_ACCOUNTS_DIR', str(acc))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(here))
    monkeypatch.setattr(cli, 'load_settings', lambda: config.Settings())
    (acc / 'active').write_text('alpha\n')
    statusline.save(_payload(90), here)
    assert cli._safe_cloud_data()['five_hour']['utilization'] == 90
    (acc / 'active').write_text('beta\n')
    assert cli._safe_cloud_data() is None


def test_token_polling_is_off_by_default_and_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')
    assert config.load_settings().token_polling is False
    config.save_settings(config.Settings(token_polling=True))
    assert config.load_settings().token_polling is True
    assert json.loads((tmp_path / 'config.json').read_text())['token_polling'] is True


def test_the_snapshot_takes_a_newer_status_line_reading(tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_ACCOUNTS_DIR', str(tmp_path))
    (tmp_path / 'alpha.json').write_text(json.dumps(
        {'email': 'a@example.com', 'claudeAiOauth': {'accessToken': 'tok-A'}}))
    (tmp_path / 'active').write_text('alpha\n')
    at = datetime.now(timezone.utc)
    line = claudehop.Reading(55.0, None, 20.0, None, None, at, 'statusline')
    snap = claudehop.take_snapshot('tok-A', {}, {'alpha': line})
    assert snap.cached['alpha'].pct5 == 55.0
    assert snap.cached['alpha'].source == 'statusline'
