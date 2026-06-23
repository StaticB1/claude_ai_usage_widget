from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from .config import RATE_CARD_FILE

# (input, cache_write_5m, cache_write_1h, cache_read, output) USD per million.
PricingTuple = Tuple[float, float, float, float, float]

DEFAULT_RATE_CARD: Dict[str, PricingTuple] = {
    'claude-opus-4-7':   (15.0, 18.75, 30.0, 1.50, 75.0),
    'claude-opus-4-6':   (15.0, 18.75, 30.0, 1.50, 75.0),
    'claude-opus-4-5':   (15.0, 18.75, 30.0, 1.50, 75.0),
    'claude-sonnet-4-7': (3.0,  3.75,   6.0, 0.30, 15.0),
    'claude-sonnet-4-6': (3.0,  3.75,   6.0, 0.30, 15.0),
    'claude-sonnet-4-5': (3.0,  3.75,   6.0, 0.30, 15.0),
    'claude-haiku-4-5':  (0.80, 1.00,   1.6, 0.08,  4.0),
}
SONNET_DEFAULT: PricingTuple = (3.0, 3.75, 6.0, 0.30, 15.0)
ZERO: PricingTuple = (0.0, 0.0, 0.0, 0.0, 0.0)


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
        low = model.lower()
        if 'opus' in low:
            return self.table.get('claude-opus-4-7',
                                  (15.0, 18.75, 30.0, 1.50, 75.0))
        if 'sonnet' in low:
            return self.table.get('claude-sonnet-4-7', SONNET_DEFAULT)
        if 'haiku' in low:
            return self.table.get('claude-haiku-4-5',
                                  (0.80, 1.00, 1.6, 0.08, 4.0))
        # Router models (kimi, qwen) and <synthetic> — never invent a price.
        return ZERO

    def cost(self, model: Optional[str], inp: int, cw5m: int,
             cw1h: int, cr: int, out: int) -> float:
        p_in, p_5m, p_1h, p_cr, p_out = self.for_model(model)
        return (inp * p_in + cw5m * p_5m + cw1h * p_1h
                + cr * p_cr + out * p_out) / 1_000_000


def load_rate_card() -> RateCard:
    """Load the user override at ~/.config/.../rate_card.json on top of
    DEFAULT_RATE_CARD. The file lets users override pricing without editing
    code when Anthropic changes a number."""
    if RATE_CARD_FILE.exists():
        try:
            data = json.loads(RATE_CARD_FILE.read_text())
            override: Dict[str, PricingTuple] = {}
            for model, rates in (data.get('models') or {}).items():
                if isinstance(rates, list) and len(rates) == 5:
                    override[model] = tuple(float(x) for x in rates)  # type: ignore
                elif isinstance(rates, dict):
                    override[model] = (
                        float(rates.get('input', 0)),
                        float(rates.get('cache_write_5m', 0)),
                        float(rates.get('cache_write_1h', 0)),
                        float(rates.get('cache_read', 0)),
                        float(rates.get('output', 0)),
                    )
            merged = {**DEFAULT_RATE_CARD, **override}
            return RateCard(merged, updated_at=data.get('updated_at'))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pass
    return RateCard(DEFAULT_RATE_CARD)


def save_rate_card(card: RateCard) -> None:
    RATE_CARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'updated_at': card.updated_at or datetime.now(timezone.utc).isoformat(),
        'models': {m: list(rates) for m, rates in card.table.items()},
    }
    RATE_CARD_FILE.write_text(json.dumps(payload, indent=2))
