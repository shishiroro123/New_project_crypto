"""Live runner: polling loop that evaluates the strategy on each closed bar.

Why polling instead of WebSocket:
- We trade on 4h bars. A decision is needed at most every 4h.
- REST polling (every N minutes) is simpler, easier to debug, and survives
  flaky network/restart cleanly.
- WS adds complexity (asyncio, reconnect, replay) for zero benefit at this horizon.

Loop pseudocode:
    every poll_interval seconds:
        for each symbol:
            fetch latest OHLCV (last ~max_lookback bars)
            if a new bar has closed since last evaluation:
                compute signals on the full window (regime-aware)
                resolve position transitions:
                    flat -> long  => submit buy + persist OpenPosition + alert
                    long -> flat  => submit sell + record ClosedTrade + alert
                check intrabar stop on open positions using last close
        record equity snapshot
        emit heartbeat
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

import ccxt
import pandas as pd

from crypto_bot.alerts import TelegramAlerter
from crypto_bot.backtest.runner import CostModel
from crypto_bot.config import BotConfig, ExecutionMode, Secrets
from crypto_bot.data.exchange import make_exchange
from crypto_bot.data.market_data import fetch_history
from crypto_bot.execution.base import ExecutorBase, OrderRequest, Side
from crypto_bot.execution.live import LiveExecutor
from crypto_bot.execution.paper import PaperExecutor
from crypto_bot.logging_setup import get_logger
from crypto_bot.risk.sizing import SizingParams, position_size
from crypto_bot.state import ClosedTrade, OpenPosition, StateStore
from crypto_bot.strategy.donchian import DonchianParams, generate_signals
from crypto_bot.strategy.indicators import align_regime, regime_filter

log = get_logger(__name__)


def _timeframe_to_seconds(tf: str) -> int:
    units = {"m": 60, "h": 3600, "d": 86400}
    return int(tf[:-1]) * units[tf[-1]]


@dataclass
class RunnerStats:
    started_at: datetime
    cycles: int = 0
    last_cycle: datetime | None = None
    fills: int = 0
    errors: int = 0


class LiveRunner:
    def __init__(
        self,
        config: BotConfig,
        secrets: Secrets,
        store: StateStore,
        executor: ExecutorBase,
        alerter: TelegramAlerter,
        client: ccxt.binance,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.cfg = config
        self.secrets = secrets
        self.store = store
        self.executor = executor
        self.alerter = alerter
        self.client = client
        self.clock = clock

        self.sizing = SizingParams(
            risk_per_trade=config.risk.risk_per_trade,
            max_position_pct=config.risk.max_position_pct,
        )
        self.params = DonchianParams(
            entry_lookback=config.strategy.entry_lookback,
            exit_lookback=config.strategy.exit_lookback,
            atr_period=config.strategy.atr_period,
            atr_stop_multiplier=config.strategy.atr_stop_multiplier,
        )

        self.stats = RunnerStats(started_at=self.clock())
        self._stop = False
        self._last_bar_ts: dict[str, pd.Timestamp] = {}

    def request_stop(self, *_: object) -> None:
        log.info("runner.stop_requested")
        self._stop = True

    def _fetch_window(self, symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
        tf_secs = _timeframe_to_seconds(timeframe)
        start = self.clock() - timedelta(seconds=tf_secs * bars)
        data_dir = Path(self.secrets.data_dir)
        return fetch_history(self.client, symbol, timeframe, start, self.clock(), data_dir)

    def _compute_regime(self) -> pd.Series | None:
        if not self.cfg.strategy.regime.enabled:
            return None
        rcfg = self.cfg.strategy.regime
        ref = self._fetch_window(rcfg.reference_symbol, rcfg.reference_timeframe, rcfg.ma_period + 5)
        if ref.empty:
            return None
        return regime_filter(ref["close"], rcfg.ma_period)

    def _evaluate_symbol(
        self,
        symbol: str,
        regime_series: pd.Series | None,
        equity: float,
    ) -> None:
        bars_needed = max(
            self.cfg.strategy.entry_lookback,
            self.cfg.strategy.exit_lookback,
            self.cfg.strategy.atr_period,
        ) + 5
        df = self._fetch_window(symbol, self.cfg.strategy.timeframe, bars_needed)
        if df.empty:
            log.warning("runner.no_data", symbol=symbol)
            return

        # The last row may correspond to an OPEN bar — skip it to avoid look-ahead.
        # Binance returns bars whose timestamp is the OPEN; a bar is closed when
        # the current time has passed open + timeframe.
        tf_secs = _timeframe_to_seconds(self.cfg.strategy.timeframe)
        now = self.clock()
        closed_mask = df.index + pd.Timedelta(seconds=tf_secs) <= pd.Timestamp(now)
        df_closed = df.loc[closed_mask]
        if df_closed.empty:
            return

        last_ts = df_closed.index[-1]
        prev_last = self._last_bar_ts.get(symbol)
        if prev_last is not None and last_ts <= prev_last:
            return  # no new bar to evaluate

        regime_aligned = None
        if regime_series is not None:
            regime_aligned = align_regime(df_closed.index, regime_series)

        signals = generate_signals(df_closed, self.params, regime_aligned)

        # Current decision = the LAST row's target position.
        last_row = signals.iloc[-1]
        prev_pos = self.store.get_position(symbol)
        in_pos = prev_pos is not None

        log.info(
            "runner.evaluation",
            symbol=symbol,
            bar=str(last_ts),
            close=float(last_row["close"]),
            target_position=float(last_row["position"]),
            in_position=in_pos,
        )

        target_long = last_row["position"] > 0.5

        if not in_pos and target_long:
            self._open_position(symbol, signals, equity)
        elif in_pos and not target_long:
            self._close_position(symbol, prev_pos, signals, reason="signal")
        elif in_pos and prev_pos is not None:
            # Still long: check stop trip.
            low = float(last_row["low"])
            if low <= prev_pos.stop_price:
                self._close_position(symbol, prev_pos, signals, reason="stop")

        self._last_bar_ts[symbol] = last_ts

    def _open_position(self, symbol: str, signals: pd.DataFrame, equity: float) -> None:
        last_row = signals.iloc[-1]
        entry_ref = float(last_row["close"])
        stop_px = (
            float(last_row["stop_price"])
            if pd.notna(last_row["stop_price"])
            else entry_ref - 2 * float(last_row["atr"])
        )
        qty = position_size(equity, entry_ref, stop_px, self.sizing)
        if qty <= 0:
            log.warning("runner.sizing_zero", symbol=symbol, equity=equity, entry=entry_ref, stop=stop_px)
            return

        fill = self.executor.submit(
            OrderRequest(symbol=symbol, side=Side.BUY, qty=qty, reference_price=entry_ref)
        )
        self.stats.fills += 1
        pos = OpenPosition(
            symbol=symbol,
            qty=fill.qty,
            entry_price=fill.price,
            stop_price=stop_px,
            entry_time=fill.timestamp,
            fees_paid=fill.fee,
        )
        self.store.upsert_position(pos)
        self.alerter.notify_entry(symbol, fill.qty, fill.price, stop_px)

    def _close_position(
        self,
        symbol: str,
        pos: OpenPosition,
        signals: pd.DataFrame,
        reason: str,
    ) -> None:
        last_row = signals.iloc[-1]
        exit_ref = float(last_row["close"])
        fill = self.executor.submit(
            OrderRequest(symbol=symbol, side=Side.SELL, qty=pos.qty, reference_price=exit_ref)
        )
        self.stats.fills += 1
        pnl = (fill.price - pos.entry_price) * pos.qty - fill.fee - pos.fees_paid
        pnl_pct = pnl / (pos.entry_price * pos.qty) if pos.qty else 0.0
        trade = ClosedTrade(
            symbol=symbol,
            entry_time=pos.entry_time,
            exit_time=fill.timestamp,
            entry_price=pos.entry_price,
            exit_price=fill.price,
            qty=pos.qty,
            pnl=pnl,
            pnl_pct=pnl_pct,
            fees=pos.fees_paid + fill.fee,
            reason=reason,
        )
        self.store.record_trade(trade)
        self.store.close_position(symbol)
        self.alerter.notify_exit(trade)

    def _current_equity(self) -> float:
        # For paper: free quote + sum(open positions marked to last close).
        # For live: actual exchange free balance.
        base_balance = self.executor.get_free_quote_balance("USDT")
        positions = self.store.all_positions()
        if not positions:
            return base_balance
        unrealized = 0.0
        for pos in positions:
            try:
                # Use last close from the cached parquet if available.
                df = self._fetch_window(pos.symbol, self.cfg.strategy.timeframe, 2)
                if df.empty:
                    continue
                last_close = float(df["close"].iloc[-1])
                unrealized += pos.qty * last_close
            except Exception as exc:  # noqa: BLE001
                log.warning("runner.mtm_failed", symbol=pos.symbol, error=str(exc))
        return base_balance + unrealized

    def run(self, poll_interval: int = 300, once: bool = False) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        log.info(
            "runner.start",
            executor=self.executor.name(),
            symbols=self.cfg.strategy.symbols,
            timeframe=self.cfg.strategy.timeframe,
            poll_interval=poll_interval,
        )
        self.store.set_meta("started_at", self.stats.started_at.isoformat())

        while not self._stop:
            cycle_t0 = self.clock()
            try:
                regime = self._compute_regime()
                equity = self._current_equity()
                self.store.record_equity(cycle_t0, equity)
                for symbol in self.cfg.strategy.symbols:
                    self._evaluate_symbol(symbol, regime, equity)
                self.store.mark_heartbeat()
                self.stats.cycles += 1
                self.stats.last_cycle = cycle_t0
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                log.error("runner.cycle_failed", error=str(exc))
                self.alerter.notify_error("runner_cycle", str(exc))

            if once:
                return

            # Sleep, but wake early if stop requested.
            for _ in range(poll_interval):
                if self._stop:
                    break
                time.sleep(1)

        log.info("runner.stop", cycles=self.stats.cycles, fills=self.stats.fills)


def build_runner(
    config: BotConfig,
    secrets: Secrets,
    store_path: Path | str = "data/state.sqlite",
) -> LiveRunner:
    store = StateStore(store_path)
    alerter = TelegramAlerter(secrets.telegram_bot_token, secrets.telegram_chat_id)

    if config.execution.mode == ExecutionMode.LIVE:
        client = make_exchange(secrets=secrets, testnet=False)
        executor: ExecutorBase = LiveExecutor(client)
    elif config.execution.mode == ExecutionMode.TESTNET:
        client = make_exchange(secrets=secrets, testnet=True)
        executor = LiveExecutor(client)
    else:
        client = make_exchange(secrets=secrets, testnet=False)  # public market data only
        cost = CostModel(
            fee_rate=config.execution.fee_rate, slippage_bps=config.execution.slippage_bps
        )
        existing = store.latest_equity()
        starting = existing[1] if existing else config.risk.initial_capital
        executor = PaperExecutor(cost, starting_balance=starting)

    return LiveRunner(
        config=config,
        secrets=secrets,
        store=store,
        executor=executor,
        alerter=alerter,
        client=client,
    )
