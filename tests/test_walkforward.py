"""Unit tests for the walk-forward analyzer (no network)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_bot.backtest.runner import CostModel
from crypto_bot.risk.sizing import SizingParams
from crypto_bot.walkforward import walk_forward


def _trending_history(n: int = 600, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = np.full(n, 0.001)
    noise = rng.normal(0, 0.01, size=n)
    close = 100.0 * np.exp(np.cumsum(drift + noise))
    high = close * (1 + rng.uniform(0.001, 0.01, size=n))
    low = close * (1 - rng.uniform(0.001, 0.01, size=n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": 1.0},
        index=idx,
    )


def test_walk_forward_rejects_empty_dataframe():
    with pytest.raises(ValueError, match="empty"):
        walk_forward(
            df=pd.DataFrame(), regime=None, n_folds=3,
            sizing=SizingParams(), cost=CostModel(),
            initial_capital=500.0, timeframe="1d",
        )


def test_walk_forward_rejects_too_few_bars():
    df = _trending_history(n=20)  # tiny — fold_size would be < 50
    with pytest.raises(ValueError, match="not enough data"):
        walk_forward(
            df=df, regime=None, n_folds=10,
            sizing=SizingParams(), cost=CostModel(),
            initial_capital=500.0, timeframe="1d",
        )


def test_walk_forward_returns_one_fold_per_n():
    df = _trending_history(n=600)
    result = walk_forward(
        df=df, regime=None, n_folds=3,
        sizing=SizingParams(risk_per_trade=0.01, max_position_pct=0.5),
        cost=CostModel(),
        initial_capital=500.0,
        timeframe="1d",
        entry_grid=[15, 20],
        exit_grid=[8, 10],
    )
    assert len(result.folds) == 3
    for fold in result.folds:
        assert fold.is_start < fold.is_end
        assert fold.is_end <= fold.oos_start
        assert fold.oos_start < fold.oos_end
        assert "sharpe" in fold.is_metrics
        assert "sharpe" in fold.oos_metrics


def test_walk_forward_combined_oos_concatenates_fold_curves():
    df = _trending_history(n=900)
    result = walk_forward(
        df=df, regime=None, n_folds=3,
        sizing=SizingParams(), cost=CostModel(),
        initial_capital=500.0, timeframe="1d",
        entry_grid=[20], exit_grid=[10],
    )
    # Combined curve length = sum of per-fold OOS lengths.
    expected_len = sum(
        len(df.iloc[(k + 1) * (len(df) // 4): (k + 2) * (len(df) // 4)])
        for k in range(3)
    )
    assert len(result.combined_oos.equity_curve) == expected_len
