from cct.cloud import extract_model_limits


# A response shaped like the live /api/oauth/usage payload: a flat `limits`
# array mixing account-wide windows with per-model (weekly_scoped) caps.
SAMPLE = {
    "five_hour": {"utilization": 15.0, "resets_at": "2026-07-22T06:19:59Z"},
    "seven_day": {"utilization": 44.0, "resets_at": "2026-07-25T03:59:59Z"},
    "limits": [
        {"group": "session", "kind": "session", "percent": 15,
         "resets_at": "2026-07-22T06:19:59Z", "scope": None,
         "is_active": False},
        {"group": "weekly", "kind": "weekly_all", "percent": 44,
         "resets_at": "2026-07-25T03:59:59Z", "scope": None,
         "is_active": False},
        {"group": "weekly", "kind": "weekly_scoped", "percent": 52,
         "resets_at": "2026-07-25T03:59:59Z", "is_active": True,
         "scope": {"model": {"display_name": "Fable", "id": None},
                   "surface": None}},
    ],
}


def test_extracts_only_weekly_scoped():
    out = extract_model_limits(SAMPLE)
    assert len(out) == 1
    m = out[0]
    assert m["model"] == "Fable"
    assert m["pct"] == 52
    assert m["fraction"] == 0.52
    assert m["is_active"] is True
    assert m["resets_at"] == "2026-07-25T03:59:59Z"


def test_sorted_highest_first():
    data = {"limits": [
        {"kind": "weekly_scoped", "percent": 10, "is_active": False,
         "scope": {"model": {"display_name": "Sonnet"}}},
        {"kind": "weekly_scoped", "percent": 80, "is_active": True,
         "scope": {"model": {"display_name": "Opus"}}},
        {"kind": "weekly_scoped", "percent": 40, "is_active": False,
         "scope": {"model": {"display_name": "Haiku"}}},
    ]}
    assert [m["model"] for m in extract_model_limits(data)] == \
        ["Opus", "Haiku", "Sonnet"]


def test_percent_normalized_and_clamped():
    data = {"limits": [
        {"kind": "weekly_scoped", "percent": 250,
         "scope": {"model": {"display_name": "Runaway"}}},
    ]}
    out = extract_model_limits(data)
    assert out[0]["fraction"] == 1.0
    assert out[0]["pct"] == 100


def test_missing_or_empty_limits():
    assert extract_model_limits({}) == []
    assert extract_model_limits({"limits": None}) == []
    assert extract_model_limits({"limits": []}) == []
    # Older API responses predate the `limits` array entirely.
    assert extract_model_limits({"five_hour": {"utilization": 5.0}}) == []


def test_non_dict_input():
    assert extract_model_limits(None) == []
    assert extract_model_limits("nope") == []
    assert extract_model_limits([1, 2, 3]) == []


def test_tolerates_malformed_entries():
    data = {"limits": [
        "not-a-dict",
        {"kind": "weekly_scoped"},                       # no scope
        {"kind": "weekly_scoped", "scope": None},        # null scope
        {"kind": "weekly_scoped", "scope": {}},          # no model
        {"kind": "weekly_scoped", "scope": {"model": None}},
        {"kind": "weekly_scoped",                        # blank display_name
         "scope": {"model": {"display_name": ""}}},
        {"kind": "weekly_scoped", "percent": 33,         # the one good row
         "scope": {"model": {"display_name": "Opus"}}},
    ]}
    out = extract_model_limits(data)
    assert [m["model"] for m in out] == ["Opus"]
    assert out[0]["pct"] == 33


def test_missing_percent_defaults_to_zero():
    data = {"limits": [
        {"kind": "weekly_scoped", "is_active": False,
         "scope": {"model": {"display_name": "Opus"}}},
    ]}
    out = extract_model_limits(data)
    assert out[0]["fraction"] == 0.0
    assert out[0]["pct"] == 0
    assert out[0]["is_active"] is False
