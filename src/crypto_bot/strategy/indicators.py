"""Pure-pandas indicators. No external TA library to keep the dependency tree small."""

from __future__ import annotations

import numpy as np
import pandas as pd


def donchian_high(high: pd.Series, period: int) -> pd.Series:
    # Use prior bars only (shift 1) so a breakout signal at bar t is based on bars [t-period, t-1].
    return high.rolling(window=period, min_periods=period).max().shift(1)


def donchian_low(low: pd.Series, period: int) -> pd.Series:
    return low.rolling(window=period, min_periods=period).min().shift(1)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's ATR (RMA of true range)."""
    tr = true_range(high, low, close)
    # Wilder smoothing == EMA with alpha = 1/period.
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def add_indicators(
    df: pd.DataFrame,
    entry_lookback: int,
    exit_lookback: int,
    atr_period: int,
) -> pd.DataFrame:
    """Return a copy with Donchian + ATR columns. Input must have OHLCV columns."""
    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"missing OHLCV columns: {sorted(missing)}")

    out = df.copy()
    out["donchian_high"] = donchian_high(out["high"], entry_lookback)
    out["donchian_low"] = donchian_low(out["low"], exit_lookback)
    out["atr"] = atr(out["high"], out["low"], out["close"], atr_period)
    return out


def regime_filter(reference_close: pd.Series, ma_period: int) -> pd.Series:
    """Boolean series: True when close > SMA(ma_period). Index = reference timeframe."""
    ma = sma(reference_close, ma_period)
    return (reference_close > ma).astype(bool)


def align_regime(
    target_index: pd.DatetimeIndex, regime: pd.Series, allow_lookahead: bool = False
) -> pd.Series:
    """Re-index a (typically daily) regime series onto a finer timeframe.

    By default we forward-fill the LAST KNOWN regime value, which avoids look-ahead:
    at bar t on the target timeframe, we only know the regime for the last *closed*
    reference bar (so we shift the reference by 1 before forward-fill).
    """
    if not allow_lookahead:
        regime = regime.shift(1)
    aligned = regime.reindex(target_index, method="ffill")
    return aligned.fillna(False).astype(bool)
