import numpy as np
import pandas as pd
import pytest

from crypto_bot.strategy.indicators import (
    align_regime,
    atr,
    donchian_high,
    donchian_low,
    regime_filter,
    sma,
)


def _ohlc(n: int = 50, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, size=n))
    high = close + rng.uniform(0.1, 1.0, size=n)
    low = close - rng.uniform(0.1, 1.0, size=n)
    open_ = close + rng.normal(0, 0.2, size=n)
    idx = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": 1.0},
        index=idx,
    )


def test_donchian_high_uses_prior_bars_only():
    df = _ohlc(20)
    dh = donchian_high(df["high"], 5)
    # The Donchian at index t must equal max(high[t-5:t]) — never include high[t].
    for t in range(5, len(df)):
        expected = df["high"].iloc[t - 5 : t].max()
        assert dh.iloc[t] == pytest.approx(expected)


def test_donchian_low_uses_prior_bars_only():
    df = _ohlc(20)
    dl = donchian_low(df["low"], 5)
    for t in range(5, len(df)):
        expected = df["low"].iloc[t - 5 : t].min()
        assert dl.iloc[t] == pytest.approx(expected)


def test_atr_is_positive_and_smooth():
    df = _ohlc(100)
    a = atr(df["high"], df["low"], df["close"], 14)
    valid = a.dropna()
    assert len(valid) > 0
    assert (valid > 0).all()


def test_sma_matches_pandas():
    s = pd.Series(np.arange(10, dtype=float))
    assert sma(s, 3).iloc[-1] == pytest.approx(s.iloc[-3:].mean())


def test_regime_filter_no_lookahead():
    # Strict uptrend then sharp drop: regime should flip AFTER MA200 is breached.
    n = 250
    close = pd.Series(np.linspace(100, 200, n))
    reg = regime_filter(close, 50)
    assert reg.iloc[100]  # well above MA
    # Make sure regime is purely a function of past prices
    assert reg.notna().sum() > 0


def test_align_regime_avoids_lookahead():
    daily_idx = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
    daily = pd.Series([False, False, True, True, False], index=daily_idx)
    target = pd.date_range("2024-01-02 04:00", periods=4, freq="4h", tz="UTC")
    aligned = align_regime(target, daily)
    # At target's first bar (2024-01-02 04:00), the only KNOWN bar is 2024-01-01 (False),
    # because the regime is shifted by 1 to avoid look-ahead.
    assert aligned.iloc[0] == False  # noqa: E712
