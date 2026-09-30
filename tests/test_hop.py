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


def test_usage_text_live_seen_reset_and_never(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    st = hop.load_state('tok-A')
    now = datetime.now(timezone.utc)

    def iso(dt):
        return dt.isoformat()

    seen = {'pct5': 34, 'pct7': 61, 'resets_5h': iso(now + timedelta(hours=2)),
            'resets_7d': iso(now + timedelta(days=3)),
            'seen_at': iso(now - timedelta(minutes=5))}
    assert hop.usage_text('alpha', st, 40, 62, True, seen, now) == (
        '5-hour 40% · 7-day 62%', 'live')
    assert hop.usage_text('alpha', st, 40, 62, False, seen, now) == (
        '5-hour 34% · 7-day 61%', 'last seen 5m ago')
    old = dict(seen, resets_5h=iso(now - timedelta(hours=1)),
               seen_at=iso(now - timedelta(hours=3)))
    assert hop.usage_text('beta', st, 40, 62, True, old, now) == (
        '5-hour reset · 7-day 61%', 'last seen 3h ago')
    assert hop.usage_text('beta', st, None, None, False, None, now) == (
        'no reading yet', '')
