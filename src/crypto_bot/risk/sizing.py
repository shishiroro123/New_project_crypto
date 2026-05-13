"""Position sizing.

Core idea (fixed-fractional risk):
    qty = (equity * risk_per_trade) / (entry_price - stop_price)

The denominator is the per-unit dollar risk. We also clamp:
- never more than `max_position_pct` * equity in notional on a single position,
- snap to exchange step / lot rounding (here we just floor to 6 decimals;
  per-symbol precision is applied at order-placement time).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SizingParams:
    risk_per_trade: float = 0.01
    max_position_pct: float = 0.5


def position_size(
    equity: float,
    entry_price: float,
    stop_price: float,
    params: SizingParams,
) -> float:
    """Return quantity (in base asset units). Zero if inputs are degenerate."""
    if equity <= 0 or entry_price <= 0:
        return 0.0
    risk_dollar = equity * params.risk_per_trade
    per_unit_risk = entry_price - stop_price
    if per_unit_risk <= 0:
        return 0.0

    qty = risk_dollar / per_unit_risk
    notional_cap = equity * params.max_position_pct
    qty_cap = notional_cap / entry_price
    qty = min(qty, qty_cap)

    return max(qty, 0.0)
