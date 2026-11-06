import json

import pytest

from cct.cli import _cutoff
from cct.store import Store


def test_cutoff_known_and_custom_periods():
    assert _cutoff('all') is None
    assert _cutoff('today') is not None
    assert _cutoff('5h') is not None
    assert _cutoff('7d') is not None
    assert _cutoff('30d') is not None
    assert _cutoff('3d') is not None
    assert _cutoff('12h') is not None


def test_cutoff_huge_value_raises_clean_systemexit():
    """A gigantic count must produce the intended SystemExit, not an
    uncaught OverflowError out of timedelta()."""
    with pytest.raises(SystemExit):
        _cutoff('99999999999d')


def test_cutoff_unicode_digits_raise_clean_systemexit():
    """str.isdigit() accepts superscripts that int() rejects — the guard uses
    isdecimal()+isascii(), so these degrade to a clean SystemExit."""
    with pytest.raises(SystemExit):
        _cutoff('²d')     # superscript two
    with pytest.raises(SystemExit):
        _cutoff('2²d')


def test_cutoff_garbage_raises_systemexit():
    with pytest.raises(SystemExit):
        _cutoff('bogus')
    with pytest.raises(SystemExit):
        _cutoff('d')


class _ListArgs:
    action = 'list'
    json = True


def test_budget_list_asks_for_the_plan_window_reset(monkeypatch, tmp_path,
                                                    capsys):
    """A 5h/7d budget measures the plan window, so the CLI has to ask for the
    reset moment — the same cloud data the dashboard uses. Otherwise the two
    front ends report different numbers for the same budget."""
    from cct import cli
    store = Store(tmp_path / 'h.db')
    store.add_budget('5h cap', 'global', '5h', limit_usd=6.0,
                     limit_tokens=None)
    monkeypatch.setattr(cli, 'Store', lambda *a, **k: store)
    asked = []

    def fake_cloud_data(*a, **k):
        asked.append(True)
        return {'five_hour': {'utilization': 50,
                              'resets_at': '2099-01-01T09:30:00Z'}}

    monkeypatch.setattr(cli, '_safe_cloud_data', fake_cloud_data)
    cli.cmd_budget(_ListArgs())
    assert asked, 'the CLI did not ask for the plan window reset'
    states = json.loads(capsys.readouterr().out)
    assert states[0]['period_key'] == '5h@2099-01-01T09:30'
