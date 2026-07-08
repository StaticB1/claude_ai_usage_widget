import pytest

from cct.cli import _cutoff


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
