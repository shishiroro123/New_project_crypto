"""Donchian breakout signal generator with optional regime filter.

Signals are produced bar-by-bar but in a vectorised way so the same logic
serves both backtesting and live decisioning (just feed the live OHLCV frame).

Rules:
- Long entry at bar t (executed at open of t+1, to avoid look-ahead) when:
    high[t] > donchian_high[t]            (break of N-bar high computed on bars t-N..t-1)
    AND regime_ok[t] (if regime filter enabled)
- Long exit at bar t (executed at open of t+1) when EITHER:
    low[t] < donchian_low[t]              (break of M-bar low computed on bars t-M..t-1)
    OR stop hit: low[t] <= stop_price     (stop set at entry - atr_stop_multiplier * ATR(entry))

This module produces a `signal` series (1.0 long, 0.0 flat) and per-trade
stop levels; actual P&L computation happens in the backtester.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from crypto_bot.strategy.indicators import add_indicators


@dataclass(frozen=True)
class DonchianParams:
    entry_lookback: int = 20
    exit_lookback: int = 10
    atr_period: int = 14
    atr_stop_multiplier: float = 2.0


def generate_signals(
    df: pd.DataFrame,
    params: DonchianParams,
    regime_ok: pd.Series | None = None,
) -> pd.DataFrame:
    """Return df augmented with: donchian_high/low, atr, position, entry_price, stop_price.

    `position` is the *target* position for the NEXT bar (lagged on purpose).
    """
    enriched = add_indicators(df, params.entry_lookback, params.exit_lookback, params.atr_period)

    if regime_ok is None:
        regime_ok = pd.Series(True, index=enriched.index)
    else:
        regime_ok = regime_ok.reindex(enriched.index).fillna(False).astype(bool)

    high = enriched["high"].to_numpy()
    low = enriched["low"].to_numpy()
    dh = enriched["donchian_high"].to_numpy()
    dl = enriched["donchian_low"].to_numpy()
    atr_arr = enriched["atr"].to_numpy()
    regime = regime_ok.to_numpy()

    n = len(enriched)
    position = np.zeros(n, dtype=np.float64)
    entry_price = np.full(n, np.nan)
    stop_price = np.full(n, np.nan)

    in_pos = False
    cur_entry = np.nan
    cur_stop = np.nan

    for t in range(n):
        if not in_pos:
            # Entry signal at t -> executed t+1 (we mark target for t+1 below).
            if (
                np.isfinite(dh[t])
                and np.isfinite(atr_arr[t])
                and regime[t]
                and high[t] > dh[t]
            ):
                in_pos = True
                # Conservative entry assumption: fill at the breakout level itself.
                # The backtester replaces this with the actual fill on the next open.
                cur_entry = dh[t]
                cur_stop = cur_entry - params.atr_stop_multiplier * atr_arr[t]
        else:
            # Exit if Donchian-low breach OR stop hit on this bar.
            exit_now = False
            if np.isfinite(dl[t]) and low[t] < dl[t]:
                exit_now = True
            if np.isfinite(cur_stop) and low[t] <= cur_stop:
                exit_now = True
            if exit_now:
                in_pos = False
                cur_entry = np.nan
                cur_stop = np.nan

        # Lag the target by one bar (executed on next open).
        if t + 1 < n:
            position[t + 1] = 1.0 if in_pos else 0.0
            entry_price[t + 1] = cur_entry
            stop_price[t + 1] = cur_stop

    enriched["position"] = position
    enriched["entry_price"] = entry_price
    enriched["stop_price"] = stop_price
    return enriched
