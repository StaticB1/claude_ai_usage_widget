"""Per-model rate card.

Prices are USD per million tokens, taken from Anthropic's published pricing
table (checked 2026-08-20). Cache rates are the documented multipliers on the
base input price: 1.25x for a 5-minute write, 2x for a 1-hour write, 0.1x for
a read — so ``rates()`` derives them rather than repeating four numbers per
model and letting them drift apart.

Override any of it without touching code by writing
``~/.config/claude-token-tracker/rate_card.json``; see ``load_rate_card``.
"""
from __future__ import annotations
import json
import re
from typing import Dict, Optional, Tuple

from .config import RATE_CARD_FILE

# (input, cache_write_5m, cache_write_1h, cache_read, output) USD per million.
PricingTuple = Tuple[float, float, float, float, float]

CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
CACHE_READ_MULTIPLIER = 0.1


def rates(base_input: float, output: float) -> PricingTuple:
    """Full five-rate tuple from a model's base input and output price."""
    return (
        base_input,
        round(base_input * CACHE_WRITE_5M_MULTIPLIER, 6),
        round(base_input * CACHE_WRITE_1H_MULTIPLIER, 6),
        round(base_input * CACHE_READ_MULTIPLIER, 6),
        output,
    )


# Base (input, output) prices per million tokens.
_BASE_PRICES: Dict[str, Tuple[float, float]] = {
    'claude-fable-5':      (10.0, 50.0),
    'claude-mythos-5':     (10.0, 50.0),
    'claude-mythos-preview': (10.0, 50.0),
    'claude-opus-5':       (5.0, 25.0),
    'claude-opus-4-8':     (5.0, 25.0),
    'claude-opus-4-7':     (5.0, 25.0),
    'claude-opus-4-6':     (5.0, 25.0),
    'claude-opus-4-5':     (5.0, 25.0),
    'claude-opus-4-1':     (15.0, 75.0),
    'claude-opus-4-0':     (15.0, 75.0),
    'claude-sonnet-5':     (2.0, 10.0),
    'claude-sonnet-4-6':   (3.0, 15.0),
    'claude-sonnet-4-5':   (3.0, 15.0),
    'claude-sonnet-4-0':   (3.0, 15.0),
    'claude-haiku-4-5':    (1.0, 5.0),
    'claude-3-5-haiku':    (0.80, 4.0),
    'claude-3-haiku':      (0.25, 1.25),
}

DEFAULT_RATE_CARD: Dict[str, PricingTuple] = {
    model: rates(inp, out) for model, (inp, out) in _BASE_PRICES.items()
}

# Newest priced member of each family, for models whose exact id we don't
# carry — a new point release usually lands at its predecessor's price.
FAMILY_FALLBACKS = (
    ('mythos', 'claude-mythos-5'),
    ('fable',  'claude-fable-5'),
    ('opus',   'claude-opus-5'),
    ('sonnet', 'claude-sonnet-5'),
    ('haiku',  'claude-haiku-4-5'),
)

SONNET_DEFAULT: PricingTuple = DEFAULT_RATE_CARD['claude-sonnet-5']
ZERO: PricingTuple = (0.0, 0.0, 0.0, 0.0, 0.0)

# Claude Code logs some models with a dated snapshot suffix
# (claude-haiku-4-5-20251001). Strip it so the alias matches exactly instead
# of falling through to a family guess.
_DATE_SUFFIX = re.compile(r'-\d{8}$')


def normalize_model(model: Optional[str]) -> str:
    """Lower-cased model id with any trailing ``-YYYYMMDD`` snapshot removed."""
    if not model:
        return ''
    return _DATE_SUFFIX.sub('', str(model).strip().lower())


class RateCard:
    def __init__(self, table: Dict[str, PricingTuple],
                 updated_at: Optional[str] = None):
        self.table: Dict[str, PricingTuple] = dict(table)
        self.updated_at = updated_at

    def for_model(self, model: Optional[str]) -> PricingTuple:
        if not model:
            return SONNET_DEFAULT
        if model in self.table:
            return self.table[model]
        key = normalize_model(model)
        if key in self.table:
            return self.table[key]
        for needle, alias in FAMILY_FALLBACKS:
            if needle in key:
                return self.table.get(alias, DEFAULT_RATE_CARD[alias])
        # Router models (kimi, qwen) and <synthetic> — never invent a price.
        return ZERO

    def is_exact(self, model: Optional[str]) -> bool:
        """True when ``model`` matched a real entry rather than a family guess.

        Lets callers flag a cost figure as approximate instead of presenting a
        guessed rate as though it were billed.
        """
        if not model:
            return False
        return model in self.table or normalize_model(model) in self.table

    def cost(self, model: Optional[str], inp: int, cw5m: int,
             cw1h: int, cr: int, out: int) -> float:
        p_in, p_5m, p_1h, p_cr, p_out = self.for_model(model)
        return (inp * p_in + cw5m * p_5m + cw1h * p_1h
                + cr * p_cr + out * p_out) / 1_000_000


def load_rate_card() -> RateCard:
    """Load the user override at ~/.config/.../rate_card.json on top of
    DEFAULT_RATE_CARD, so pricing can be corrected without editing code.

    Each entry is either a 5-element list in rate-tuple order, a dict of named
    rates, or a dict carrying just ``input`` and ``output`` — in which case the
    cache rates are derived from the standard multipliers.
    """
    if not RATE_CARD_FILE.exists():
        return RateCard(DEFAULT_RATE_CARD)
    try:
        data = json.loads(RATE_CARD_FILE.read_text())
        override: Dict[str, PricingTuple] = {}
        for model, entry in (data.get('models') or {}).items():
            if isinstance(entry, list) and len(entry) == 5:
                override[model] = tuple(float(x) for x in entry)  # type: ignore
            elif isinstance(entry, dict):
                base_in = float(entry.get('input', 0))
                out = float(entry.get('output', 0))
                derived = rates(base_in, out)
                override[model] = (
                    base_in,
                    float(entry.get('cache_write_5m', derived[1])),
                    float(entry.get('cache_write_1h', derived[2])),
                    float(entry.get('cache_read', derived[3])),
                    out,
                )
        return RateCard({**DEFAULT_RATE_CARD, **override},
                        updated_at=data.get('updated_at'))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return RateCard(DEFAULT_RATE_CARD)
