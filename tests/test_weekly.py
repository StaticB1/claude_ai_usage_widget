from cct.budgets import _utilization_pct
from cct.cloud import extract_model_limits, weekly_window
from cct.notifications import WindowSnapshot

# A Team seat's reply, as measured on 13 Oct 2026: seven_day is null and the
# weekly cap is a model-scoped row in `limits`.
TEAM = {
    'five_hour': {'utilization': 84.0, 'resets_at': '2026-10-13T06:50:00+00:00'},
    'seven_day': None,
    'limits': [
        {'kind': 'session', 'group': 'session', 'percent': 84.0},
        {'kind': 'weekly_scoped', 'group': 'weekly', 'percent': 9.0,
         'resets_at': '2026-10-17T00:00:00+00:00',
         'scope': {'model': {'display_name': 'Fable'}}},
    ],
}


def test_plans_with_a_seven_day_window_keep_it():
    data = {'seven_day': {'utilization': 40, 'resets_at': 'x'},
            'limits': [{'kind': 'weekly_scoped', 'percent': 90,
                        'scope': {'model': {'display_name': 'Opus'}}}]}
    assert weekly_window(data) == {'utilization': 40, 'resets_at': 'x', 'scope': None}


def test_team_seat_uses_its_fullest_weekly_meter():
    assert weekly_window(TEAM) == {'utilization': 9.0,
                                   'resets_at': '2026-10-17T00:00:00+00:00',
                                   'scope': 'Fable'}


def test_an_all_model_weekly_row_beats_a_fuller_scoped_one():
    data = {'seven_day': None, 'limits': [
        {'kind': 'weekly_scoped', 'percent': 70,
         'scope': {'model': {'display_name': 'Fable'}}},
        {'kind': 'weekly_all', 'group': 'weekly', 'percent': 20, 'resets_at': 'r'}]}
    assert weekly_window(data) == {'utilization': 20.0, 'resets_at': 'r', 'scope': None}


def test_no_weekly_figure_at_all():
    assert weekly_window({'five_hour': {'utilization': 1}, 'seven_day': None}) is None
    assert weekly_window(None) is None


def test_surface_scoped_limits_are_named():
    data = {'limits': [{'kind': 'weekly_scoped', 'percent': 3,
                        'scope': {'surface': {'display_name': 'Claude Code'}}}]}
    assert [m['model'] for m in extract_model_limits(data)] == ['Claude Code']


def test_notifications_and_budgets_see_the_team_week():
    snap = WindowSnapshot.from_cloud_data(TEAM)
    assert (snap.five_hour_pct, snap.seven_day_pct) == (84.0, 9.0)
    assert _utilization_pct(TEAM, '7d') == 9.0
