"""Rate-card tests.

Every figure asserted here comes from Anthropic's published pricing table
(checked 2026-08-20). If a test fails after a rate-card edit, check the
published price before changing the expectation.
"""
from cct.pricing import (DEFAULT_RATE_CARD, ZERO, RateCard, normalize_model,
                         rates)


def card() -> RateCard:
    return RateCard(DEFAULT_RATE_CARD)


def test_published_input_and_output_prices():
    rc = card()
    # 1M input / 1M output, per the published table.
    assert rc.cost('claude-opus-5', 1_000_000, 0, 0, 0, 0) == 5.0
    assert rc.cost('claude-opus-5', 0, 0, 0, 0, 1_000_000) == 25.0
    assert rc.cost('claude-fable-5', 1_000_000, 0, 0, 0, 0) == 10.0
    assert rc.cost('claude-fable-5', 0, 0, 0, 0, 1_000_000) == 50.0
    assert rc.cost('claude-sonnet-5', 1_000_000, 0, 0, 0, 0) == 2.0
    assert rc.cost('claude-sonnet-5', 0, 0, 0, 0, 1_000_000) == 10.0
    assert rc.cost('claude-haiku-4-5', 1_000_000, 0, 0, 0, 0) == 1.0
    assert rc.cost('claude-haiku-4-5', 0, 0, 0, 0, 1_000_000) == 5.0


def test_opus_4x_family_is_five_dollars_not_fifteen():
    """Opus 4.5 through 4.8 are $5/$25, not the $15/$75 of Opus 4.1."""
    rc = card()
    for model in ('claude-opus-4-5', 'claude-opus-4-6',
                  'claude-opus-4-7', 'claude-opus-4-8'):
        assert rc.cost(model, 1_000_000, 0, 0, 0, 0) == 5.0, model
        assert rc.cost(model, 0, 0, 0, 0, 1_000_000) == 25.0, model
    assert rc.cost('claude-opus-4-1', 1_000_000, 0, 0, 0, 0) == 15.0


def test_cache_multipliers():
    """5m write = 1.25x input, 1h write = 2x, read = 0.1x."""
    rc = card()
    p_in, p_5m, p_1h, p_cr, _ = rc.for_model('claude-opus-5')
    assert (p_5m, p_1h, p_cr) == (p_in * 1.25, p_in * 2, p_in * 0.1)
    assert rates(4.0, 20.0) == (4.0, 5.0, 8.0, 0.4, 20.0)


def test_full_split_sums_every_rate():
    rc = card()
    cost = rc.cost('claude-opus-5', inp=1_000_000, cw5m=1_000_000,
                   cw1h=1_000_000, cr=1_000_000, out=1_000_000)
    # 5 + 6.25 + 10 + 0.50 + 25
    assert abs(cost - 46.75) < 1e-9


def test_dated_snapshot_ids_match_exactly():
    """A logged `-YYYYMMDD` snapshot must hit its own entry, not a guess."""
    rc = card()
    assert normalize_model('claude-haiku-4-5-20251001') == 'claude-haiku-4-5'
    assert rc.for_model('claude-haiku-4-5-20251001') == \
        rc.for_model('claude-haiku-4-5')
    assert rc.is_exact('claude-haiku-4-5-20251001')
    assert rc.is_exact('claude-opus-5')


def test_unknown_variant_falls_back_to_newest_in_family():
    rc = card()
    assert rc.for_model('claude-sonnet-9-9') == rc.for_model('claude-sonnet-5')
    assert rc.for_model('claude-opus-9-9') == rc.for_model('claude-opus-5')
    # ...and is reported as approximate, not billed-exact.
    assert not rc.is_exact('claude-opus-9-9')


def test_router_models_are_priced_zero():
    rc = card()
    assert rc.for_model('kimi-k2') == ZERO
    assert rc.for_model('<synthetic>') == ZERO
    assert rc.cost('kimi-k2', 1_000_000, 0, 0, 0, 1_000_000) == 0.0


def test_user_override_wins():
    override = dict(DEFAULT_RATE_CARD)
    override['claude-opus-5'] = (10.0, 12.5, 20.0, 1.0, 50.0)
    rc = RateCard(override)
    assert rc.cost('claude-opus-5', 1_000_000, 0, 0, 0, 0) == 10.0
