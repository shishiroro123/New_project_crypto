"""Single-symbol event-driven backtester (loop over bars).

It is deliberately simple (one symbol, long-only, one position at a time) to
match the MVP strategy and keep the logic auditable. A multi-symbol version
that splits capital across signals lives in `portfolio.py` (TODO).

Pricing assumptions:
- Entries and exits fill at the NEXT bar's open (signals are lagged in the
  strategy module, so when `position[t] == 1` and `position[t-1] == 0`, we buy
  at open[t]).
- Slippage = `slippage_bps` against us on every fill.
- Fees = `fee_rate` on the notional of every fill (taker).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from crypto_bot.logging_setup import get_logger
from crypto_bot.risk.sizing import SizingParams, position_size

log = get_logger(__name__)


@dataclass(frozen=True)
class CostModel:
    fee_rate: float = 0.001
    slippage_bps: float = 5.0

    def buy_price(self, mid: float) -> float:
        return mid * (1 + self.slippage_bps / 10_000)

    def sell_price(self, mid: float) -> float:
        return mid * (1 - self.slippage_bps / 10_000)


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp | None = None
    entry_price: float = 0.0
    exit_price: float = 0.0
    qty: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    fees: float = 0.0
    reason: str = ""


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trades: list[Trade] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


def _annualization_factor(timeframe: str) -> float:
    units = {"m": 60, "h": 3600, "d": 86400}
    unit = timeframe[-1]
    if unit not in units:
        return float(np.sqrt(365))
    seconds = int(timeframe[:-1]) * units[unit]
    bars_per_year = 365 * 24 * 3600 / seconds
    return float(np.sqrt(bars_per_year))


def compute_metrics(equity: pd.Series, timeframe: str) -> dict[str, float]:
    if equity.empty:
        return {}
    returns = equity.pct_change().dropna()
    if returns.empty or returns.std() == 0:
        sharpe = 0.0
    else:
        sharpe = float(returns.mean() / returns.std() * _annualization_factor(timeframe))
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    years = max((equity.index[-1] - equity.index[0]).total_seconds() / (365 * 86400), 1e-9)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)
    return {
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
        "n_bars": float(len(equity)),
    }


def run_backtest(
    df: pd.DataFrame,
    initial_capital: float,
    sizing: SizingParams,
    cost: CostModel,
    timeframe: str,
    max_drawdown_kill: float | None = None,
) -> BacktestResult:
    """Run a long-only single-symbol backtest on the output of `generate_signals`."""
    required = {"open", "high", "low", "close", "position", "stop_price"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"missing strategy columns: {sorted(missing)}")

    open_ = df["open"].to_numpy()
    low = df["low"].to_numpy()
    high = df["high"].to_numpy()
    close = df["close"].to_numpy()
    pos = df["position"].to_numpy()
    stop = df["stop_price"].to_numpy()
    times = df.index

    equity = initial_capital
    qty = 0.0
    in_pos = False
    open_trade: Trade | None = None
    trades: list[Trade] = []
    equity_curve = np.empty(len(df))
    halted = False

    for t in range(len(df)):
        prev = pos[t - 1] if t > 0 else 0.0
        cur = pos[t]

        # Entry: transition 0 -> 1, fill at this bar's open.
        if not in_pos and prev == 0.0 and cur == 1.0 and not halted:
            entry = cost.buy_price(open_[t])
            stop_px = stop[t] if np.isfinite(stop[t]) else entry * 0.95
            q = position_size(equity, entry, stop_px, sizing)
            if q > 0:
                fees = entry * q * cost.fee_rate
                equity -= fees
                qty = q
                in_pos = True
                open_trade = Trade(
                    entry_time=times[t],
                    entry_price=entry,
                    qty=qty,
                    fees=fees,
                )

        # Exit: transition 1 -> 0, fill at this bar's open (strategy-triggered).
        # Also handle intrabar stop: if low <= stop while still in position, exit at stop.
        if in_pos and open_trade is not None:
            exit_now = False
            exit_px = np.nan
            reason = ""
            if prev == 1.0 and cur == 0.0:
                exit_now = True
                exit_px = cost.sell_price(open_[t])
                reason = "signal"
            elif np.isfinite(stop[t]) and low[t] <= stop[t]:
                exit_now = True
                exit_px = cost.sell_price(stop[t])
                reason = "stop"

            if exit_now:
                fees = exit_px * qty * cost.fee_rate
                pnl = (exit_px - open_trade.entry_price) * qty - fees - open_trade.fees
                equity += (exit_px - open_trade.entry_price) * qty - fees
                open_trade.exit_time = times[t]
                open_trade.exit_price = exit_px
                open_trade.pnl = pnl
                open_trade.pnl_pct = pnl / (open_trade.entry_price * qty) if qty else 0.0
                open_trade.fees += fees
                open_trade.reason = reason
                trades.append(open_trade)
                qty = 0.0
                in_pos = False
                open_trade = None

        # Mark-to-market equity for the curve.
        mtm = equity + (qty * close[t] - (qty * open_trade.entry_price if open_trade else 0.0)
                        if in_pos and open_trade else 0.0)
        equity_curve[t] = mtm

        # Risk kill switch.
        if max_drawdown_kill is not None and not halted:
            peak = np.nanmax(equity_curve[: t + 1])
            if peak > 0 and (mtm / peak - 1.0) <= -abs(max_drawdown_kill):
                log.warning(
                    "backtest.dd_kill_triggered",
                    bar=int(t),
                    drawdown=float(mtm / peak - 1.0),
                )
                halted = True
                if in_pos and open_trade is not None:
                    exit_px = cost.sell_price(close[t])
                    fees = exit_px * qty * cost.fee_rate
                    pnl = (exit_px - open_trade.entry_price) * qty - fees - open_trade.fees
                    equity += (exit_px - open_trade.entry_price) * qty - fees
                    open_trade.exit_time = times[t]
                    open_trade.exit_price = exit_px
                    open_trade.pnl = pnl
                    open_trade.pnl_pct = (
                        pnl / (open_trade.entry_price * qty) if qty else 0.0
                    )
                    open_trade.fees += fees
                    open_trade.reason = "dd_kill"
                    trades.append(open_trade)
                    qty = 0.0
                    in_pos = False
                    open_trade = None

    eq_series = pd.Series(equity_curve, index=times, name="equity")
    metrics = compute_metrics(eq_series, timeframe)
    metrics["n_trades"] = float(len(trades))
    if trades:
        wins = [t for t in trades if t.pnl > 0]
        metrics["win_rate"] = len(wins) / len(trades)
        metrics["avg_trade_pnl"] = float(np.mean([t.pnl for t in trades]))
    return BacktestResult(equity_curve=eq_series, trades=trades, metrics=metrics)
