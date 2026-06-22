from datetime import datetime, timedelta, timezone

from cct.notifications import (NotificationManager, WindowSnapshot,
                               _is_rollover, _threshold_level,
                               LEVEL_NONE, LEVEL_WARN, LEVEL_CRITICAL,
                               LEVEL_MAX)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def test_threshold_level_steps():
    assert _threshold_level(10, 60, 85) == LEVEL_NONE
    assert _threshold_level(60, 60, 85) == LEVEL_WARN
    assert _threshold_level(70, 60, 85) == LEVEL_WARN
    assert _threshold_level(85, 60, 85) == LEVEL_CRITICAL
    assert _threshold_level(99, 60, 85) == LEVEL_CRITICAL
    assert _threshold_level(100, 60, 85) == LEVEL_MAX


def test_rollover_detection_5h_tolerates_jitter():
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    assert not _is_rollover(_iso(base), _iso(base + timedelta(minutes=30)),
                            tolerance_seconds=3600)
    assert _is_rollover(_iso(base), _iso(base + timedelta(hours=2)),
                        tolerance_seconds=3600)


def test_rollover_detection_7d_tolerates_drift():
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    assert not _is_rollover(_iso(base), _iso(base + timedelta(hours=4)),
                            tolerance_seconds=6 * 3600)
    assert _is_rollover(_iso(base), _iso(base + timedelta(hours=10)),
                        tolerance_seconds=6 * 3600)


def test_threshold_escalates_then_does_not_refire(tmp_path):
    mgr = NotificationManager(state_file=tmp_path / 'state.json')
    reset = _iso(datetime(2026, 5, 1, 17, tzinfo=timezone.utc))
    snap = WindowSnapshot(five_hour_pct=70, five_hour_resets_at=reset)
    notes = mgr.evaluate('default', snap)
    levels = [n.level for n in notes if n.window == '5h']
    assert levels == ['warn']

    # Same window, still 70% — should not re-fire.
    notes = mgr.evaluate('default', snap)
    assert [n.level for n in notes if n.window == '5h'] == []

    # Cross to critical — only the critical fires (warn already done).
    snap2 = WindowSnapshot(five_hour_pct=90, five_hour_resets_at=reset)
    notes = mgr.evaluate('default', snap2)
    assert [n.level for n in notes if n.window == '5h'] == ['critical']

    # Hit 100% — only max fires.
    snap3 = WindowSnapshot(five_hour_pct=100, five_hour_resets_at=reset)
    notes = mgr.evaluate('default', snap3)
    assert [n.level for n in notes if n.window == '5h'] == ['max']

    # Same data again — silence.
    notes = mgr.evaluate('default', snap3)
    assert [n.level for n in notes if n.window == '5h'] == []


def test_threshold_skips_levels_when_jumping(tmp_path):
    mgr = NotificationManager(state_file=tmp_path / 'state.json')
    reset = _iso(datetime(2026, 5, 1, 17, tzinfo=timezone.utc))
    snap = WindowSnapshot(five_hour_pct=95, five_hour_resets_at=reset)
    notes = mgr.evaluate('default', snap)
    # Sudden jump should still emit warn AND critical to leave a clear trail.
    levels = [n.level for n in notes if n.window == '5h']
    assert levels == ['warn', 'critical']


def test_window_rollover_rearms(tmp_path):
    mgr = NotificationManager(state_file=tmp_path / 'state.json')
    r1 = _iso(datetime(2026, 5, 1, 17, tzinfo=timezone.utc))
    snap = WindowSnapshot(five_hour_pct=90, five_hour_resets_at=r1)
    mgr.evaluate('default', snap)
    notes = mgr.evaluate('default', snap)
    assert notes == []  # already fired

    # New window starts (resets_at shifts well past tolerance) — re-arm.
    r2 = _iso(datetime(2026, 5, 1, 22, tzinfo=timezone.utc))
    notes = mgr.evaluate('default', WindowSnapshot(
        five_hour_pct=90, five_hour_resets_at=r2))
    levels = [n.level for n in notes if n.window == '5h']
    assert levels == ['warn', 'critical']


def test_state_persists_across_managers(tmp_path):
    state = tmp_path / 'state.json'
    reset = _iso(datetime(2026, 5, 1, 17, tzinfo=timezone.utc))
    snap = WindowSnapshot(five_hour_pct=90, five_hour_resets_at=reset)
    NotificationManager(state_file=state).evaluate('default', snap)
    # Fresh manager loads state — still suppressed.
    notes = NotificationManager(state_file=state).evaluate('default', snap)
    assert notes == []


def test_burn_rate_grace_period_suppresses_early_alerts(tmp_path):
    mgr = NotificationManager(state_file=tmp_path / 'state.json')
    # 7d window resets in 7 days minus 1 hour — so only 1h has elapsed.
    now = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    resets = _iso(now + timedelta(days=7) - timedelta(hours=1))
    snap = WindowSnapshot(seven_day_pct=50, seven_day_resets_at=resets)
    notes = mgr.evaluate('default', snap,
                         burn_rate={'enabled': True, 'multiplier': 1.5},
                         now=now)
    # Threshold notification still fires (50% > warn=60? no, 50 < 60),
    # so no threshold either. But burn-rate must be silent in grace.
    assert [n for n in notes if n.kind == 'burn_rate'] == []


def test_burn_rate_fires_after_grace(tmp_path):
    mgr = NotificationManager(state_file=tmp_path / 'state.json')
    # 7d window: 24h elapsed (past 8h grace), 50% used → pace ≈ 50/14% ≈ 3.5×
    now = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    resets = _iso(now + timedelta(days=7) - timedelta(hours=24))
    snap = WindowSnapshot(seven_day_pct=50, seven_day_resets_at=resets)
    notes = mgr.evaluate('default', snap,
                         burn_rate={'enabled': True, 'multiplier': 1.5},
                         now=now)
    burn = [n for n in notes if n.kind == 'burn_rate']
    assert len(burn) == 1
    assert burn[0].level == 'early'  # below warn=60

    # Same conditions: don't re-fire at the same level.
    notes = mgr.evaluate('default', snap,
                         burn_rate={'enabled': True, 'multiplier': 1.5},
                         now=now)
    assert [n for n in notes if n.kind == 'burn_rate'] == []


def test_burn_rate_ignored_when_disabled(tmp_path):
    mgr = NotificationManager(state_file=tmp_path / 'state.json')
    now = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    resets = _iso(now + timedelta(days=7) - timedelta(hours=24))
    snap = WindowSnapshot(seven_day_pct=80, seven_day_resets_at=resets)
    notes = mgr.evaluate('default', snap,
                         burn_rate={'enabled': False, 'multiplier': 1.5},
                         now=now)
    assert [n for n in notes if n.kind == 'burn_rate'] == []


def test_separate_accounts_isolated(tmp_path):
    mgr = NotificationManager(state_file=tmp_path / 'state.json')
    reset = _iso(datetime(2026, 5, 1, 17, tzinfo=timezone.utc))
    snap = WindowSnapshot(five_hour_pct=70, five_hour_resets_at=reset)
    a = mgr.evaluate('work', snap)
    b = mgr.evaluate('personal', snap)
    assert [n.account for n in a] == ['work']
    assert [n.account for n in b] == ['personal']


def test_snapshot_from_cloud_data_handles_fraction_or_percent():
    s1 = WindowSnapshot.from_cloud_data({
        'five_hour': {'utilization': 0.42, 'resets_at': 'x'},
        'seven_day': {'utilization': 73, 'resets_at': 'y'},
    })
    assert s1.five_hour_pct == 42
    assert s1.seven_day_pct == 73
    assert WindowSnapshot.from_cloud_data(None).five_hour_pct is None
