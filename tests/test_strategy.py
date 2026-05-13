import numpy as np
import pandas as pd

from crypto_bot.strategy.donchian import DonchianParams, generate_signals


def _make_breakout_series() -> pd.DataFrame:
    """Construct OHLC where bars 0..19 are flat at 100, then bar 20 breaks out."""
    n = 40
    idx = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    high = np.full(n, 100.5)
    low = np.full(n, 99.5)
    close = np.full(n, 100.0)
    open_ = np.full(n, 100.0)
    # Inject a breakout at t=20.
    high[20] = 110.0
    close[20] = 109.0
    # Make it sustain so no immediate exit.
    for i in range(21, 30):
        high[i] = 110.0 + i * 0.1
        low[i] = 108.0
        close[i] = 109.0 + i * 0.1
        open_[i] = 109.0
    # Then break the exit Donchian-low at t=35.
    for i in range(30, n):
        high[i] = 100.0
        low[i] = 90.0
        close[i] = 95.0
        open_[i] = 100.0
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": 1.0},
        index=idx,
    )


def test_breakout_triggers_entry_lagged_by_one_bar():
    df = _make_breakout_series()
    params = DonchianParams(entry_lookback=10, exit_lookback=5, atr_period=5)
    signals = generate_signals(df, params)
    # At bar 20 we detect the breakout, position is set for bar 21.
    assert signals["position"].iloc[20] == 0.0
    assert signals["position"].iloc[21] == 1.0


def test_breakout_exits_on_low_break():
    df = _make_breakout_series()
    params = DonchianParams(entry_lookback=10, exit_lookback=5, atr_period=5)
    signals = generate_signals(df, params)
    # Eventually we should flip back to flat after the price collapses.
    assert signals["position"].iloc[-1] == 0.0


def test_no_entry_when_regime_off():
    df = _make_breakout_series()
    params = DonchianParams(entry_lookback=10, exit_lookback=5, atr_period=5)
    regime = pd.Series(False, index=df.index)
    signals = generate_signals(df, params, regime_ok=regime)
    assert (signals["position"] == 0.0).all()
