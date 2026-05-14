"""Live runner: polling loop that evaluates the strategy on each closed bar.

Why polling instead of WebSocket:
- We trade on daily/hourly bars. A decision is needed at most once per bar.
- REST polling (every N minutes) is simpler, easier to debug, and survives
  flaky network/restart cleanly.
- WS adds complexity (asyncio, reconnect, replay) for zero benefit at this horizon.

Per-cycle behaviour:
    for each symbol:
        if open position: check stop via live ticker (intracycle protection)
        else: check max_concurrent_positions + cooldown after last stop
        if a NEW bar has closed since last evaluation:
            compute signals on the full window (regime-aware)
            resolve position transitions:
                flat -> long  => submit buy + persist OpenPosition + alert
                long -> flat  => submit sell + record ClosedTrade (atomic) + alert
    record equity snapshot
    if drawdown > max_drawdown_kill: flatten + halt the bot
    emit heartbeat
"""

from __future__ import annotations

import re
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


def _safe_symbol(symbol: str) -> str:
    """Symbol stripped to alphanumerics — for use in clientOrderId."""
    return re.sub(r"[^A-Za-z0-9]", "", symbol)


def _client_order_id(symbol: str, side: Side, bar_ts: datetime, salt: str = "") -> str:
    """Deterministic ID: same (symbol, side, bar, salt) always yields the same string.

    Binance accepts up to 36 chars [A-Za-z0-9-_]. We keep well under that.
    """
    ts_ms = int(bar_ts.timestamp() * 1000)
    base = f"bot-{_safe_symbol(symbol)}-{ts_ms}-{side.value}"
    if salt:
        base = f"{base}-{salt}"
    return base[:36]


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
        client: ccxt.Exchange,
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

    # -- lifecycle -----------------------------------------------------------

    def request_stop(self, *_: object) -> None:
        log.info("runner.stop_requested")
        self._stop = True

    # -- data ----------------------------------------------------------------

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

    def _live_price(self, symbol: str) -> float | None:
        """Best-effort last-trade price. Used for intracycle stop and kill flatten."""
        try:
            ticker = self.client.fetch_ticker(symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("runner.ticker_failed", symbol=symbol, error=str(exc))
            return None
        for key in ("last", "close", "bid"):
            v = ticker.get(key) if isinstance(ticker, dict) else None
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    # -- intracycle stop ----------------------------------------------------

    def _check_intracycle_stop(self, pos: OpenPosition) -> bool:
        """If the live price has hit the stop, close immediately. Returns True if closed."""
        last = self._live_price(pos.symbol)
        if last is None or last > pos.stop_price:
            return False

        log.warning(
            "runner.intracycle_stop_hit",
            symbol=pos.symbol,
            last=last,
            stop=pos.stop_price,
        )
        fill = self.executor.submit(
            OrderRequest(
                symbol=pos.symbol,
                side=Side.SELL,
                qty=pos.qty,
                reference_price=last,
                client_order_id=_client_order_id(pos.symbol, Side.SELL, self.clock(), "stop"),
            )
        )
        self.stats.fills += 1
        pnl = (fill.price - pos.entry_price) * pos.qty - fill.fee - pos.fees_paid
        pnl_pct = pnl / (pos.entry_price * pos.qty) if pos.qty else 0.0
        trade = ClosedTrade(
            symbol=pos.symbol,
            entry_time=pos.entry_time,
            exit_time=fill.timestamp,
            entry_price=pos.entry_price,
            exit_price=fill.price,
            qty=pos.qty,
            pnl=pnl,
            pnl_pct=pnl_pct,
            fees=pos.fees_paid + fill.fee,
            reason="stop_intracycle",
        )
        # Atomic: append trade + delete open-position row in a single SQL tx.
        self.store.record_trade_and_close_position(trade)
        # Anti-churn cooldown: don't re-enter the same symbol for one bar period.
        tf_secs = _timeframe_to_seconds(self.cfg.strategy.timeframe)
        cooldown_until = self.clock() + timedelta(seconds=tf_secs)
        self.store.set_meta(f"cooldown_until_{pos.symbol}", cooldown_until.isoformat())
        self.alerter.notify_exit(trade)
        return True

    def _in_cooldown(self, symbol: str) -> bool:
        v = self.store.get_meta(f"cooldown_until_{symbol}")
        if not v:
            return False
        try:
            until = datetime.fromisoformat(v)
        except ValueError:
            return False
        return self.clock() < until

    # -- bar-driven evaluation ----------------------------------------------

    def _evaluate_symbol(self, symbol: str, regime_series: pd.Series | None) -> None:
        bars_needed = max(
            self.cfg.strategy.entry_lookback,
            self.cfg.strategy.exit_lookback,
            self.cfg.strategy.atr_period,
        ) + 5
        df = self._fetch_window(symbol, self.cfg.strategy.timeframe, bars_needed)
        if df.empty:
            log.warning("runner.no_data", symbol=symbol)
            return

        # Drop the last bar if it isn't closed yet to avoid look-ahead.
        tf_secs = _timeframe_to_seconds(self.cfg.strategy.timeframe)
        now = self.clock()
        closed_mask = df.index + pd.Timedelta(seconds=tf_secs) <= pd.Timestamp(now)
        df_closed = df.loc[closed_mask]
        if df_closed.empty:
            return

        last_ts = df_closed.index[-1]
        prev_last = self._last_bar_ts.get(symbol)
        new_bar = prev_last is None or last_ts > prev_last
        if not new_bar:
            return

        regime_aligned = (
            align_regime(df_closed.index, regime_series) if regime_series is not None else None
        )
        signals = generate_signals(df_closed, self.params, regime_aligned)
        last_row = signals.iloc[-1]
        target_long = bool(last_row["position"] > 0.5)
        prev_pos = self.store.get_position(symbol)

        log.info(
            "runner.evaluation",
            symbol=symbol,
            bar=str(last_ts),
            close=float(last_row["close"]),
            target_position=float(last_row["position"]),
            in_position=prev_pos is not None,
        )

        if prev_pos is None and target_long:
            if self._in_cooldown(symbol):
                log.info("runner.entry_skipped_cooldown", symbol=symbol)
            elif self._open_slots_available():
                self._open_position(symbol, signals)
        elif prev_pos is not None and not target_long:
            # Bar-close exit signal.
            self._close_position(symbol, prev_pos, signals, reason="signal")
        elif prev_pos is not None:
            # Bar-close stop check (intrabar stops are caught by _check_intracycle_stop).
            low = float(last_row["low"])
            if low <= prev_pos.stop_price:
                self._close_position(symbol, prev_pos, signals, reason="stop")

        self._last_bar_ts[symbol] = last_ts

    def _open_slots_available(self) -> bool:
        n_open = len(self.store.all_positions())
        return n_open < self.cfg.risk.max_concurrent_positions

    def _open_position(self, symbol: str, signals: pd.DataFrame) -> None:
        # Refresh equity right before sizing so two symbols opening in the same
        # cycle don't both size against the pre-trade balance.
        equity = self._current_equity()

        last_row = signals.iloc[-1]
        entry_ref = float(last_row["close"])
        atr = float(last_row["atr"]) if pd.notna(last_row["atr"]) else 0.0
        if atr <= 0:
            log.warning("runner.entry_skipped_no_atr", symbol=symbol)
            return

        bar_ts = signals.index[-1].to_pydatetime()
        cid = _client_order_id(symbol, Side.BUY, bar_ts)
        # Provisional stop anchored to the close, used for sizing only.
        # The DEFINITIVE stop is anchored to the actual fill price below.
        provisional_stop = entry_ref - self.params.atr_stop_multiplier * atr
        qty = position_size(equity, entry_ref, provisional_stop, self.sizing)
        if qty <= 0:
            log.warning(
                "runner.sizing_zero", symbol=symbol, equity=equity, entry=entry_ref,
                stop=provisional_stop,
            )
            return

        fill = self.executor.submit(
            OrderRequest(
                symbol=symbol,
                side=Side.BUY,
                qty=qty,
                reference_price=entry_ref,
                client_order_id=cid,
            )
        )
        self.stats.fills += 1
        # Re-anchor the stop to the ACTUAL fill price. Risk per trade is now
        # exact even if slippage moved us off the signal level.
        stop_px = fill.price - self.params.atr_stop_multiplier * atr
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
        bar_ts = signals.index[-1].to_pydatetime()
        cid = _client_order_id(symbol, Side.SELL, bar_ts)
        fill = self.executor.submit(
            OrderRequest(
                symbol=symbol,
                side=Side.SELL,
                qty=pos.qty,
                reference_price=exit_ref,
                client_order_id=cid,
            )
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
        self.store.record_trade_and_close_position(trade)
        self.alerter.notify_exit(trade)

    # -- equity & kill switch ----------------------------------------------

    def _current_equity(self) -> float:
        base_balance = self.executor.get_free_quote_balance("USDT")
        positions = self.store.all_positions()
        if not positions:
            return base_balance
        position_value = 0.0
        for pos in positions:
            last = self._live_price(pos.symbol)
            if last is None:
                # Fall back to last close if ticker is unavailable.
                try:
                    df = self._fetch_window(pos.symbol, self.cfg.strategy.timeframe, 2)
                    last = float(df["close"].iloc[-1]) if not df.empty else pos.entry_price
                except Exception:  # noqa: BLE001
                    last = pos.entry_price
            position_value += pos.qty * last
        return base_balance + position_value

    def _flatten_all(self, reason: str) -> None:
        for pos in self.store.all_positions():
            try:
                last = self._live_price(pos.symbol) or pos.entry_price
                cid = _client_order_id(pos.symbol, Side.SELL, self.clock(), reason[:8])
                fill = self.executor.submit(
                    OrderRequest(
                        symbol=pos.symbol,
                        side=Side.SELL,
                        qty=pos.qty,
                        reference_price=last,
                        client_order_id=cid,
                    )
                )
                self.stats.fills += 1
                pnl = (fill.price - pos.entry_price) * pos.qty - fill.fee - pos.fees_paid
                pnl_pct = pnl / (pos.entry_price * pos.qty) if pos.qty else 0.0
                trade = ClosedTrade(
                    symbol=pos.symbol,
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
                self.store.record_trade_and_close_position(trade)
                self.alerter.notify_exit(trade)
            except Exception as exc:  # noqa: BLE001
                log.error("runner.flatten_failed", symbol=pos.symbol, error=str(exc))

    def _check_kill_switch(self, current_equity: float) -> bool:
        """If drawdown breaches `max_drawdown_kill`, flatten + halt. Returns halted."""
        if self.store.is_halted():
            return True
        threshold = abs(self.cfg.risk.max_drawdown_kill)
        if threshold <= 0:
            return False
        peak = self.store.peak_equity()
        if peak is None or peak <= 0:
            peak = current_equity
        dd = current_equity / peak - 1.0
        if dd <= -threshold:
            log.error(
                "runner.kill_switch_triggered",
                equity=current_equity,
                peak=peak,
                drawdown=dd,
                threshold=-threshold,
            )
            self.alerter.notify_error(
                "kill_switch",
                f"DD {dd*100:.2f}% breached threshold {-threshold*100:.2f}% — flattening + halting",
            )
            self._flatten_all(reason="dd_kill")
            self.store.halt(reason=f"dd_kill at {dd*100:.2f}%")
            return True
        return False

    # -- main loop ----------------------------------------------------------

    def run(self, poll_interval: int = 300, once: bool = False) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        log.info(
            "runner.start",
            executor=self.executor.name(),
            symbols=self.cfg.strategy.symbols,
            timeframe=self.cfg.strategy.timeframe,
            poll_interval=poll_interval,
            halted=self.store.is_halted(),
        )
        self.store.set_meta("started_at", self.stats.started_at.isoformat())
        if self.store.is_halted():
            log.warning("runner.halted_on_startup", reason=self.store.halt_reason())
            self.alerter.notify_error(
                "halted",
                f"Bot started in HALTED state (reason: {self.store.halt_reason()}). "
                "Run `crypto-bot unhalt` after investigating to resume.",
            )

        while not self._stop:
            cycle_t0 = self.clock()
            try:
                self._cycle(cycle_t0)
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                log.error("runner.cycle_failed", error=str(exc))
                self.alerter.notify_error("runner_cycle", str(exc))

            if once:
                return

            for _ in range(poll_interval):
                if self._stop:
                    break
                time.sleep(1)

        log.info("runner.stop", cycles=self.stats.cycles, fills=self.stats.fills)

    def _cycle(self, cycle_t0: datetime) -> None:
        # 1) Intracycle stops on existing positions ALWAYS run — even when
        #    halted, we still flatten if a stop has been hit between bars.
        for pos in self.store.all_positions():
            self._check_intracycle_stop(pos)

        # 2) Mark-to-market equity, then check the kill switch BEFORE strategy
        #    evaluation. Otherwise the bar-driven exit may close the position
        #    on a "signal" reason before the kill switch can record "dd_kill".
        equity = self._current_equity()
        self.store.record_equity(cycle_t0, equity)
        halted = self.store.is_halted()
        if not halted:
            halted = self._check_kill_switch(equity)

        # 3) Bar-driven strategy evaluation only when running normally.
        if not halted:
            regime = self._compute_regime()
            for symbol in self.cfg.strategy.symbols:
                self._evaluate_symbol(symbol, regime)

        self.store.mark_heartbeat()
        self.stats.cycles += 1
        self.stats.last_cycle = cycle_t0


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
