"""CLI: `crypto-bot fetch` / `crypto-bot backtest`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from crypto_bot.backtest.runner import CostModel, run_backtest
from crypto_bot.config import BotConfig, Secrets
from crypto_bot.data.exchange import make_binance
from crypto_bot.data.market_data import fetch_history
from crypto_bot.logging_setup import configure as configure_logging
from crypto_bot.logging_setup import get_logger
from crypto_bot.risk.sizing import SizingParams
from crypto_bot.strategy.donchian import DonchianParams, generate_signals
from crypto_bot.strategy.indicators import align_regime, regime_filter

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()
log = get_logger(__name__)


def _load_config(path: Path) -> BotConfig:
    if not path.exists():
        raise typer.BadParameter(f"config not found: {path}")
    return BotConfig.from_yaml(path)


@app.callback()
def _global(
    log_level: str = typer.Option("INFO", "--log-level", help="DEBUG/INFO/WARN/ERROR"),
    json_logs: bool = typer.Option(False, "--json-logs"),
) -> None:
    configure_logging(level=log_level, json_logs=json_logs)


@app.command()
def fetch(
    config: Path = typer.Option(Path("config/default.yaml"), "--config", "-c"),
    start: str = typer.Option("2020-01-01", "--start"),
    end: str | None = typer.Option(None, "--end"),
) -> None:
    """Download (or update cached) OHLCV for all configured symbols + the regime reference."""
    cfg = _load_config(config)
    secrets = Secrets()
    client = make_binance(secrets, testnet=False)  # public endpoints don't need testnet

    start_dt = datetime.fromisoformat(start).replace(tzinfo=UTC)
    end_dt = datetime.fromisoformat(end).replace(tzinfo=UTC) if end else None
    data_dir = Path(secrets.data_dir)

    symbols = list(cfg.strategy.symbols)
    if cfg.strategy.regime.enabled:
        ref = cfg.strategy.regime.reference_symbol
        if ref not in symbols:
            symbols.append(ref)

    for sym in symbols:
        log.info("fetch.symbol_start", symbol=sym, timeframe=cfg.strategy.timeframe)
        df = fetch_history(client, sym, cfg.strategy.timeframe, start_dt, end_dt, data_dir)
        log.info("fetch.symbol_done", symbol=sym, rows=len(df))

    if cfg.strategy.regime.enabled:
        ref_tf = cfg.strategy.regime.reference_timeframe
        if ref_tf != cfg.strategy.timeframe:
            log.info(
                "fetch.regime_ref_start",
                symbol=cfg.strategy.regime.reference_symbol,
                timeframe=ref_tf,
            )
            df = fetch_history(
                client,
                cfg.strategy.regime.reference_symbol,
                ref_tf,
                start_dt,
                end_dt,
                data_dir,
            )
            log.info("fetch.regime_ref_done", rows=len(df))


@app.command()
def backtest(
    config: Path = typer.Option(Path("config/default.yaml"), "--config", "-c"),
    symbol: str = typer.Option(None, "--symbol", help="Override single symbol; default = all"),
) -> None:
    """Run the backtest on each configured symbol and print metrics."""
    cfg = _load_config(config)
    secrets = Secrets()
    data_dir = Path(secrets.data_dir)
    client = make_binance(secrets, testnet=False)

    start_dt = datetime.fromisoformat(cfg.backtest.start).replace(tzinfo=UTC)
    end_dt = (
        datetime.fromisoformat(cfg.backtest.end).replace(tzinfo=UTC)
        if cfg.backtest.end
        else None
    )

    regime_series: pd.Series | None = None
    if cfg.strategy.regime.enabled:
        ref_df = fetch_history(
            client,
            cfg.strategy.regime.reference_symbol,
            cfg.strategy.regime.reference_timeframe,
            start_dt,
            end_dt,
            data_dir,
        )
        regime_series = regime_filter(ref_df["close"], cfg.strategy.regime.ma_period)

    params = DonchianParams(
        entry_lookback=cfg.strategy.entry_lookback,
        exit_lookback=cfg.strategy.exit_lookback,
        atr_period=cfg.strategy.atr_period,
        atr_stop_multiplier=cfg.strategy.atr_stop_multiplier,
    )
    sizing = SizingParams(
        risk_per_trade=cfg.risk.risk_per_trade,
        max_position_pct=cfg.risk.max_position_pct,
    )
    cost = CostModel(fee_rate=cfg.execution.fee_rate, slippage_bps=cfg.execution.slippage_bps)

    symbols = [symbol] if symbol else cfg.strategy.symbols

    results_table = Table(title="Backtest results", show_lines=True)
    results_table.add_column("symbol")
    for col in ("total_return", "cagr", "sharpe", "max_drawdown", "n_trades", "win_rate"):
        results_table.add_column(col)

    for sym in symbols:
        df = fetch_history(client, sym, cfg.strategy.timeframe, start_dt, end_dt, data_dir)
        if df.empty:
            console.print(f"[yellow]no data for {sym}, skipping[/yellow]")
            continue
        df = df.iloc[cfg.backtest.warmup_bars :]

        regime_aligned = None
        if regime_series is not None:
            regime_aligned = align_regime(df.index, regime_series)

        signals = generate_signals(df, params, regime_aligned)
        result = run_backtest(
            signals,
            initial_capital=cfg.risk.initial_capital,
            sizing=sizing,
            cost=cost,
            timeframe=cfg.strategy.timeframe,
            max_drawdown_kill=cfg.risk.max_drawdown_kill,
        )
        m = result.metrics
        results_table.add_row(
            sym,
            f"{m.get('total_return', 0):.2%}",
            f"{m.get('cagr', 0):.2%}",
            f"{m.get('sharpe', 0):.2f}",
            f"{m.get('max_drawdown', 0):.2%}",
            f"{int(m.get('n_trades', 0))}",
            f"{m.get('win_rate', 0):.2%}" if "win_rate" in m else "-",
        )

    console.print(results_table)


if __name__ == "__main__":
    app()
