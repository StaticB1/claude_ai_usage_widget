from cct.pricing import DEFAULT_RATE_CARD, RateCard, ZERO


def test_known_model():
    rc = RateCard(DEFAULT_RATE_CARD)
    # 1M input tokens on Sonnet-4-7 = $3
    assert rc.cost('claude-sonnet-4-7', 1_000_000, 0, 0, 0, 0) == 3.0
    # 1M output tokens on Opus-4-7 = $75
    assert rc.cost('claude-opus-4-7', 0, 0, 0, 0, 1_000_000) == 75.0


def test_full_split():
    rc = RateCard(DEFAULT_RATE_CARD)
    # 1M of each on Sonnet
    cost = rc.cost('claude-sonnet-4-7',
                   inp=1_000_000, cw5m=1_000_000, cw1h=1_000_000,
                   cr=1_000_000, out=1_000_000)
    # 3 + 3.75 + 6 + 0.30 + 15 = 28.05
    assert abs(cost - 28.05) < 1e-9


def test_unknown_model_falls_back_by_family():
    rc = RateCard(DEFAULT_RATE_CARD)
    # Made-up Sonnet variant should still match Sonnet rates
    assert rc.for_model('claude-sonnet-9-9') == \
           rc.for_model('claude-sonnet-4-7')


def test_router_models_are_priced_zero():
    rc = RateCard(DEFAULT_RATE_CARD)
    # Non-Anthropic / synthetic models — never invent a price
    assert rc.for_model('kimi-k2') == ZERO
    assert rc.for_model('<synthetic>') == ZERO
    assert rc.cost('kimi-k2', 1_000_000, 0, 0, 0, 1_000_000) == 0.0


def test_user_override_wins():
    override = dict(DEFAULT_RATE_CARD)
    override['claude-opus-4-7'] = (10.0, 12.5, 20.0, 1.0, 50.0)
    rc = RateCard(override)
    assert rc.cost('claude-opus-4-7', 1_000_000, 0, 0, 0, 0) == 10.0
