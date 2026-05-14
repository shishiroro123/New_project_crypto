"""LiveRunner unit tests with a fake ccxt client + paper executor.

These cover the P0 safety mechanisms added in this iteration:
- intracycle stop (live ticker triggers an exit between bars)
- kill switch (drawdown breaches halt the bot + flatten)
- max_concurrent_positions enforcement
- entry cooldown after intracycle stop (anti-churn)
- post-halt restart respects the halted flag
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from crypto_bot.alerts import TelegramAlerter
from crypto_bot.backtest.runner import CostModel
from crypto_bot.config import (
    BacktestConfig,
    BotConfig,
    ExecutionConfig,
    RegimeConfig,
    RiskConfig,
    Secrets,
    StrategyConfig,
)
from crypto_bot.execution.paper import PaperExecutor
from crypto_bot.live_runner import LiveRunner
from crypto_bot.state import OpenPosition, StateStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClient:
    """Minimal ccxt-shaped stub. Drives bar history and ticker for the runner."""

    def __init__(self, ohlcv: dict[str, pd.DataFrame], ticker: dict[str, float]) -> None:
        self._ohlcv = ohlcv
        self._ticker = ticker

    # market_data.fetch_history reaches into our .fetch_ohlcv via the wrapper.
    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        df = self._ohlcv[symbol]
        rows = []
        for ts, row in df.iterrows():
            ms = int(ts.timestamp() * 1000)
            if since is not None and ms < since:
                continue
            rows.append([ms, row["open"], row["high"], row["low"], row["close"], row["volume"]])
            if limit is not None and len(rows) >= limit:
                break
        return rows

    def fetch_ticker(self, symbol):
        return {"last": self._ticker.get(symbol, 0.0)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path) -> BotConfig:
    return BotConfig(
        strategy=StrategyConfig(
            timeframe="1d",
            symbols=["BTC/USDT"],
            entry_lookback=5,
            exit_lookback=3,
            atr_period=5,
            atr_stop_multiplier=2.0,
            regime=RegimeConfig(enabled=False),
        ),
        risk=RiskConfig(
            initial_capital=1000.0,
            risk_per_trade=0.01,
            max_concurrent_positions=1,
            max_position_pct=0.5,
            max_drawdown_kill=0.25,
        ),
        execution=ExecutionConfig(fee_rate=0.001, slippage_bps=5.0),
        backtest=BacktestConfig(),
    )


def _build_runner(tmp_path, ohlcv, ticker, clock, halted: bool = False):
    cfg = _make_config(tmp_path)
    secrets = Secrets(data_dir=str(tmp_path))
    store = StateStore(tmp_path / "state.sqlite")
    if halted:
        store.halt(reason="test")
    executor = PaperExecutor(
        CostModel(fee_rate=cfg.execution.fee_rate, slippage_bps=cfg.execution.slippage_bps),
        starting_balance=cfg.risk.initial_capital,
    )
    alerter = TelegramAlerter()  # disabled (no tokens)
    client = FakeClient(ohlcv, ticker)
    return LiveRunner(
        config=cfg,
        secrets=secrets,
        store=store,
        executor=executor,
        alerter=alerter,
        client=client,
        clock=clock,
    ), store, executor


def _flat_history(n: int = 30, price: float = 100.0,
                  end: datetime | None = None) -> pd.DataFrame:
    """Build a flat-price OHLCV history that ENDS at `end` (default: 2024-01-30).

    Using a definite end lets tests align their clock so fetch_history's
    range filter [clock-N*tf, clock) overlaps the synthetic history.
    """
    end_ts = pd.Timestamp(end or datetime(2024, 1, 30, tzinfo=UTC))
    idx = pd.date_range(end=end_ts, periods=n, freq="1D")
    return pd.DataFrame(
        {
            "open": price,
            "high": price + 0.1,
            "low": price - 0.1,
            "close": price,
            "volume": 1.0,
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_intracycle_stop_triggers_close_and_cooldown(tmp_path):
    """An open position with live price < stop must close out immediately."""
    clock_at = datetime(2024, 1, 31, tzinfo=UTC)
    runner, store, _ = _build_runner(
        tmp_path,
        ohlcv={"BTC/USDT": _flat_history()},
        ticker={"BTC/USDT": 90.0},  # well below the stop
        clock=lambda: clock_at,
    )
    store.upsert_position(
        OpenPosition(
            symbol="BTC/USDT",
            qty=1.0,
            entry_price=100.0,
            stop_price=95.0,
            entry_time=datetime(2024, 1, 25, tzinfo=UTC),
            fees_paid=0.1,
        )
    )

    runner._cycle(clock_at)

    assert store.get_position("BTC/USDT") is None, "position should be flat after intracycle stop"
    trades = store.recent_trades()
    assert len(trades) == 1
    assert trades[0].reason == "stop_intracycle"
    # Cooldown meta is set (one timeframe = 1d).
    cool = store.get_meta("cooldown_until_BTC/USDT")
    assert cool is not None


def test_intracycle_stop_not_triggered_when_above(tmp_path):
    """Direct unit test on _check_intracycle_stop: above-stop ticker = no exit."""
    runner, store, _ = _build_runner(
        tmp_path,
        ohlcv={"BTC/USDT": _flat_history()},
        ticker={"BTC/USDT": 99.0},  # above stop
        clock=lambda: datetime(2024, 1, 31, tzinfo=UTC),
    )
    pos = OpenPosition(
        symbol="BTC/USDT", qty=1.0, entry_price=100.0, stop_price=95.0,
        entry_time=datetime(2024, 1, 25, tzinfo=UTC),
    )
    store.upsert_position(pos)
    triggered = runner._check_intracycle_stop(pos)
    assert triggered is False
    assert store.get_position("BTC/USDT") is not None


def test_kill_switch_halts_and_flattens(tmp_path):
    """When equity vs peak DD exceeds the threshold, flatten + halt."""
    clock_at = datetime(2024, 1, 31, tzinfo=UTC)
    runner, store, _ = _build_runner(
        tmp_path,
        ohlcv={"BTC/USDT": _flat_history()},
        ticker={"BTC/USDT": 100.0},
        clock=lambda: clock_at,
    )
    # Seed a peak high above the current paper executor balance.
    store.record_equity(datetime(2024, 1, 1, tzinfo=UTC), 2000.0)
    # Open a position to verify it gets flattened by the kill switch.
    store.upsert_position(
        OpenPosition(
            symbol="BTC/USDT", qty=1.0, entry_price=100.0, stop_price=95.0,
            entry_time=datetime(2024, 1, 20, tzinfo=UTC),
        )
    )
    # equity now ~ 1000 cash + 1 * 100 = 1100, peak = 2000 -> DD = -45%, well past -25%.
    runner._cycle(clock_at)
    assert store.is_halted()
    # Position must have been flattened.
    assert store.get_position("BTC/USDT") is None
    trades = store.recent_trades()
    assert len(trades) == 1
    assert trades[0].reason == "dd_kill"


def test_halted_runner_skips_strategy_evaluation(tmp_path):
    """When halted, the runner still runs intracycle checks but no new entries."""
    clock_at = datetime(2024, 1, 31, tzinfo=UTC)
    runner, store, executor = _build_runner(
        tmp_path,
        ohlcv={"BTC/USDT": _flat_history()},
        ticker={"BTC/USDT": 100.0},
        clock=lambda: clock_at,
        halted=True,
    )
    starting_balance = executor.get_free_quote_balance("USDT")
    runner._cycle(clock_at)
    # Halt sticky; no position opened; balance unchanged.
    assert store.is_halted()
    assert store.get_position("BTC/USDT") is None
    assert executor.get_free_quote_balance("USDT") == pytest.approx(starting_balance)


def test_max_concurrent_positions_blocks_second_entry(tmp_path):
    """A second symbol cannot enter when max_concurrent_positions = 1."""
    cfg = _make_config(tmp_path)
    cfg.strategy.symbols = ["BTC/USDT", "ETH/USDT"]
    cfg.risk.max_concurrent_positions = 1

    # Build history with a breakout 2 bars before the last so the signal
    # is "long" on the last CLOSED bar (donchian.py lags target by one bar).
    def breakout_series() -> pd.DataFrame:
        n = 25
        end = datetime(2024, 1, 25, tzinfo=UTC)
        idx = pd.date_range(end=end, periods=n, freq="1D")
        close = np.full(n, 100.0)
        high = np.full(n, 100.5)
        low = np.full(n, 99.5)
        # Breakout 2 bars before the end ⇒ position[-1] == 1.
        high[-3] = 130.0
        close[-3] = 120.0
        # Maintain elevation so the strategy stays long.
        high[-2] = 125.0
        close[-2] = 122.0
        high[-1] = 124.0
        close[-1] = 121.0
        return pd.DataFrame(
            {"open": close, "high": high, "low": low, "close": close, "volume": 1.0},
            index=idx,
        )

    history = breakout_series()
    # Clock right after the history end so the last bar counts as CLOSED.
    clock_at = datetime(2024, 1, 26, tzinfo=UTC)
    runner, store, _ = _build_runner(
        tmp_path,
        ohlcv={"BTC/USDT": history, "ETH/USDT": history.copy()},
        ticker={"BTC/USDT": 109.0, "ETH/USDT": 109.0},
        clock=lambda: clock_at,
    )
    # Override config to the multi-symbol one.
    runner.cfg = cfg

    runner._cycle(clock_at)

    n_open = len(store.all_positions())
    assert n_open == 1, f"expected exactly 1 open position, got {n_open}"


def test_cooldown_blocks_immediate_reentry(tmp_path):
    """After intracycle stop, the bot must not re-enter on the same bar period."""
    history = _flat_history()
    # Make the LAST bar a breakout that would otherwise trigger entry.
    history.iloc[-1, history.columns.get_loc("high")] = 200.0
    history.iloc[-1, history.columns.get_loc("close")] = 195.0

    # Clock right after the history ends so the last bar is closed.
    clock_time = datetime(2024, 1, 31, tzinfo=UTC)
    runner, store, _ = _build_runner(
        tmp_path,
        ohlcv={"BTC/USDT": history},
        ticker={"BTC/USDT": 80.0},
        clock=lambda: clock_time,
    )
    store.upsert_position(
        OpenPosition(
            symbol="BTC/USDT", qty=1.0, entry_price=100.0, stop_price=95.0,
            entry_time=datetime(2024, 1, 25, tzinfo=UTC),
        )
    )
    # First cycle: intracycle stop fires, cooldown is set.
    runner._cycle(clock_time)
    assert store.get_position("BTC/USDT") is None

    # Second cycle (still within the same bar period): even if the strategy
    # signal says enter, the cooldown should block it.
    runner._cycle(clock_time)
    assert store.get_position("BTC/USDT") is None, "cooldown should prevent re-entry"
