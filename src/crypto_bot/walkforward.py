"""Walk-forward analysis: anchored, non-overlapping out-of-sample windows.

Idea: split history into N folds. For each fold, train (here = pick best
params over a small grid) on the IS window, then evaluate FROZEN params on
the next OOS window. Concatenate OOS results.

This is the simplest defense against overfit: if OOS Sharpe is significantly
worse than IS Sharpe, the strategy is fragile.

NOTE: For a Donchian breakout we deliberately keep the param grid TINY
(otherwise we just overfit the grid to the IS folds). Two knobs only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from crypto_bot.backtest.runner import BacktestResult, CostModel, run_backtest
from crypto_bot.logging_setup import get_logger
from crypto_bot.risk.sizing import SizingParams
from crypto_bot.strategy.donchian import DonchianParams, generate_signals
from crypto_bot.strategy.indicators import align_regime

log = get_logger(__name__)


@dataclass
class WalkForwardFold:
    is_start: datetime
    is_end: datetime
    oos_start: datetime
    oos_end: datetime
    chosen_params: DonchianParams
    is_metrics: dict[str, float]
    oos_metrics: dict[str, float]


@dataclass
class WalkForwardResult:
    folds: list[WalkForwardFold]
    combined_oos: BacktestResult


def _eval(
    df: pd.DataFrame,
    params: DonchianParams,
    regime: pd.Series | None,
    sizing: SizingParams,
    cost: CostModel,
    initial_capital: float,
    timeframe: str,
) -> BacktestResult:
    regime_aligned = align_regime(df.index, regime) if regime is not None else None
    signals = generate_signals(df, params, regime_aligned)
    return run_backtest(signals, initial_capital, sizing, cost, timeframe)


def walk_forward(
    df: pd.DataFrame,
    regime: pd.Series | None,
    n_folds: int,
    sizing: SizingParams,
    cost: CostModel,
    initial_capital: float,
    timeframe: str,
    entry_grid: list[int] | None = None,
    exit_grid: list[int] | None = None,
    fixed_atr_period: int = 14,
    fixed_atr_mult: float = 2.0,
) -> WalkForwardResult:
    if df.empty:
        raise ValueError("empty dataframe")
    entry_grid = entry_grid or [15, 20, 25]
    exit_grid = exit_grid or [8, 10, 12]

    fold_size = len(df) // (n_folds + 1)
    if fold_size < 50:
        raise ValueError("not enough data for the requested fold count")

    folds: list[WalkForwardFold] = []
    oos_equities: list[pd.Series] = []

    for k in range(n_folds):
        is_slice = df.iloc[: (k + 1) * fold_size]
        oos_slice = df.iloc[(k + 1) * fold_size : (k + 2) * fold_size]
        if oos_slice.empty:
            break

        # Pick the best Sharpe on IS.
        best: tuple[float, DonchianParams, BacktestResult] | None = None
        for e in entry_grid:
            for x in exit_grid:
                params = DonchianParams(
                    entry_lookback=e,
                    exit_lookback=x,
                    atr_period=fixed_atr_period,
                    atr_stop_multiplier=fixed_atr_mult,
                )
                result = _eval(is_slice, params, regime, sizing, cost, initial_capital, timeframe)
                sharpe = result.metrics.get("sharpe", 0.0)
                if best is None or sharpe > best[0]:
                    best = (sharpe, params, result)

        assert best is not None
        chosen_params = best[1]
        oos_result = _eval(oos_slice, chosen_params, regime, sizing, cost, initial_capital, timeframe)

        folds.append(
            WalkForwardFold(
                is_start=is_slice.index[0].to_pydatetime(),
                is_end=is_slice.index[-1].to_pydatetime(),
                oos_start=oos_slice.index[0].to_pydatetime(),
                oos_end=oos_slice.index[-1].to_pydatetime(),
                chosen_params=chosen_params,
                is_metrics=best[2].metrics,
                oos_metrics=oos_result.metrics,
            )
        )
        oos_equities.append(oos_result.equity_curve)

    if not oos_equities:
        raise RuntimeError("walk-forward produced no folds")

    combined_curve = pd.concat(oos_equities)
    from crypto_bot.backtest.runner import compute_metrics  # local import to avoid cycle

    combined = BacktestResult(
        equity_curve=combined_curve,
        trades=[],
        metrics=compute_metrics(combined_curve, timeframe),
    )
    return WalkForwardResult(folds=folds, combined_oos=combined)
