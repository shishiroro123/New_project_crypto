"""CLI commands: fetch / backtest / walkforward / run / news / status."""

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
from crypto_bot.live_runner import build_runner
from crypto_bot.logging_setup import configure as configure_logging
from crypto_bot.logging_setup import get_logger
from crypto_bot.news import BinanceAnnouncements, CryptoPanicFeed
from crypto_bot.risk.sizing import SizingParams
from crypto_bot.state import StateStore
from crypto_bot.strategy.donchian import DonchianParams, generate_signals
from crypto_bot.strategy.indicators import align_regime, regime_filter
from crypto_bot.walkforward import walk_forward

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


@app.command()
def walkforward(
    config: Path = typer.Option(Path("config/default.yaml"), "--config", "-c"),
    symbol: str = typer.Option("BTC/USDT", "--symbol"),
    n_folds: int = typer.Option(5, "--folds", min=2, max=20),
) -> None:
    """Anchored walk-forward analysis (out-of-sample) for one symbol."""
    cfg = _load_config(config)
    secrets = Secrets()
    data_dir = Path(secrets.data_dir)
    client = make_binance(secrets, testnet=False)

    start_dt = datetime.fromisoformat(cfg.backtest.start).replace(tzinfo=UTC)
    end_dt = (
        datetime.fromisoformat(cfg.backtest.end).replace(tzinfo=UTC) if cfg.backtest.end else None
    )

    df = fetch_history(client, symbol, cfg.strategy.timeframe, start_dt, end_dt, data_dir)
    if df.empty:
        console.print("[red]no data available[/red]")
        raise typer.Exit(code=1)
    df = df.iloc[cfg.backtest.warmup_bars :]

    regime = None
    if cfg.strategy.regime.enabled:
        ref_df = fetch_history(
            client,
            cfg.strategy.regime.reference_symbol,
            cfg.strategy.regime.reference_timeframe,
            start_dt,
            end_dt,
            data_dir,
        )
        regime = regime_filter(ref_df["close"], cfg.strategy.regime.ma_period)

    sizing = SizingParams(
        risk_per_trade=cfg.risk.risk_per_trade,
        max_position_pct=cfg.risk.max_position_pct,
    )
    cost = CostModel(fee_rate=cfg.execution.fee_rate, slippage_bps=cfg.execution.slippage_bps)

    result = walk_forward(
        df=df,
        regime=regime,
        n_folds=n_folds,
        sizing=sizing,
        cost=cost,
        initial_capital=cfg.risk.initial_capital,
        timeframe=cfg.strategy.timeframe,
    )

    folds_table = Table(title=f"Walk-forward folds ({symbol})", show_lines=True)
    folds_table.add_column("fold")
    folds_table.add_column("oos period")
    folds_table.add_column("params (e/x)")
    folds_table.add_column("IS sharpe")
    folds_table.add_column("OOS sharpe")
    folds_table.add_column("OOS return")
    folds_table.add_column("OOS DD")
    for i, fold in enumerate(result.folds, start=1):
        folds_table.add_row(
            str(i),
            f"{fold.oos_start:%Y-%m-%d}->{fold.oos_end:%Y-%m-%d}",
            f"{fold.chosen_params.entry_lookback}/{fold.chosen_params.exit_lookback}",
            f"{fold.is_metrics.get('sharpe', 0):.2f}",
            f"{fold.oos_metrics.get('sharpe', 0):.2f}",
            f"{fold.oos_metrics.get('total_return', 0):.2%}",
            f"{fold.oos_metrics.get('max_drawdown', 0):.2%}",
        )
    console.print(folds_table)

    combined = result.combined_oos.metrics
    console.print(
        f"[bold]Combined OOS:[/bold] sharpe={combined.get('sharpe', 0):.2f} "
        f"return={combined.get('total_return', 0):.2%} "
        f"max_dd={combined.get('max_drawdown', 0):.2%}"
    )


@app.command()
def run(
    config: Path = typer.Option(Path("config/default.yaml"), "--config", "-c"),
    poll_interval: int = typer.Option(300, "--poll", help="seconds between cycles"),
    once: bool = typer.Option(False, "--once", help="run a single cycle then exit"),
    state_path: Path = typer.Option(Path("data/state.sqlite"), "--state"),
) -> None:
    """Start the live runner (paper/testnet/live mode set in config)."""
    cfg = _load_config(config)
    secrets = Secrets()
    runner = build_runner(cfg, secrets, store_path=state_path)
    runner.run(poll_interval=poll_interval, once=once)


@app.command()
def news(
    symbol_filter: str = typer.Option("BTC,ETH", "--currencies", help="comma list, e.g. BTC,ETH"),
    cryptopanic_token: str = typer.Option("", "--cryptopanic-token"),
) -> None:
    """Fetch latest news items from CryptoPanic + Binance announcements."""
    ccys = [s.strip() for s in symbol_filter.split(",") if s.strip()]
    cp = CryptoPanicFeed(auth_token=cryptopanic_token, currencies=ccys).poll()
    ba = BinanceAnnouncements().poll()

    table = Table(title="Latest news", show_lines=True)
    table.add_column("source")
    table.add_column("time")
    table.add_column("currencies")
    table.add_column("title")
    for it in (cp + ba)[-25:]:
        table.add_row(
            it.source,
            it.published_at.strftime("%Y-%m-%d %H:%M"),
            ",".join(it.currencies),
            it.title[:120],
        )
    console.print(table)


@app.command()
def status(
    state_path: Path = typer.Option(Path("data/state.sqlite"), "--state"),
    n: int = typer.Option(10, "--n"),
) -> None:
    """Show open positions, latest equity, recent trades."""
    store = StateStore(state_path)
    positions = store.all_positions()
    eq = store.latest_equity()
    trades = store.recent_trades(n)

    if eq:
        console.print(f"[bold]Latest equity:[/bold] {eq[1]:.2f} (at {eq[0].isoformat()})")
    else:
        console.print("[dim]no equity snapshot yet[/dim]")

    ptable = Table(title="Open positions", show_lines=True)
    for col in ("symbol", "qty", "entry", "stop", "entry_time"):
        ptable.add_column(col)
    for p in positions:
        ptable.add_row(
            p.symbol,
            f"{p.qty:.6f}",
            f"{p.entry_price:.4f}",
            f"{p.stop_price:.4f}",
            p.entry_time.isoformat(),
        )
    console.print(ptable)

    ttable = Table(title=f"Last {n} trades", show_lines=True)
    for col in ("symbol", "entry", "exit", "qty", "pnl", "pnl_pct", "reason"):
        ttable.add_column(col)
    for t in trades:
        ttable.add_row(
            t.symbol,
            f"{t.entry_price:.4f}",
            f"{t.exit_price:.4f}",
            f"{t.qty:.6f}",
            f"{t.pnl:+.2f}",
            f"{t.pnl_pct * 100:+.2f}%",
            t.reason,
        )
    console.print(ttable)


@app.command()
def dashboard(
    state_path: Path = typer.Option(Path("data/state.sqlite"), "--state"),
    port: int = typer.Option(8501, "--port"),
    host: str = typer.Option("0.0.0.0", "--host"),
) -> None:
    """Launch the Streamlit dashboard (read-only view of state.sqlite)."""
    import shutil
    import subprocess

    streamlit_bin = shutil.which("streamlit")
    if streamlit_bin is None:
        console.print(
            "[red]streamlit is not installed.[/red] Install UI deps:\n"
            "  pip install -e '.[ui]'"
        )
        raise typer.Exit(code=1)

    app_path = Path(__file__).resolve().parents[2] / "dashboard" / "app.py"
    if not app_path.exists():
        console.print(f"[red]dashboard app not found at {app_path}[/red]")
        raise typer.Exit(code=1)

    cmd = [
        streamlit_bin,
        "run",
        str(app_path),
        "--server.port",
        str(port),
        "--server.address",
        host,
        "--browser.gatherUsageStats",
        "false",
        "--",
        "--state",
        str(state_path),
    ]
    console.print(f"[green]Starting dashboard at http://{host}:{port}[/green]")
    subprocess.run(cmd, check=False)


@app.command()
def seed_demo(
    state_path: Path = typer.Option(Path("data/state_demo.sqlite"), "--state"),
    starting_capital: float = typer.Option(500.0, "--capital"),
    n_bars: int = typer.Option(1200, "--bars"),
    seed: int = typer.Option(42, "--seed"),
) -> None:
    """Populate a demo state.sqlite with virtual trades so the dashboard has data.

    Runs a backtest on synthetic OHLCV (deterministic, seeded) and injects the
    resulting trades + equity snapshots into the chosen state DB. Use this to
    preview the dashboard before the bot has accumulated real history.
    """
    import numpy as np

    from crypto_bot.backtest.runner import CostModel, run_backtest
    from crypto_bot.risk.sizing import SizingParams
    from crypto_bot.state import ClosedTrade, StateStore
    from crypto_bot.strategy.donchian import DonchianParams, generate_signals

    rng = np.random.default_rng(seed)
    half = n_bars // 2
    drift = np.concatenate([np.full(half, 0.0012), np.full(n_bars - half, -0.0008)])
    log_returns = drift + rng.normal(0, 0.012, size=n_bars)
    close = 50_000.0 * np.exp(np.cumsum(log_returns))
    high = close * (1 + rng.uniform(0.001, 0.012, size=n_bars))
    low = close * (1 - rng.uniform(0.001, 0.012, size=n_bars))
    open_ = np.concatenate([[close[0]], close[:-1]])
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=n_bars, freq="4h")
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": 1.0},
        index=idx,
    )

    params = DonchianParams(entry_lookback=20, exit_lookback=10, atr_period=14)
    signals = generate_signals(df, params)
    result = run_backtest(
        signals,
        initial_capital=starting_capital,
        sizing=SizingParams(risk_per_trade=0.01, max_position_pct=0.5),
        cost=CostModel(fee_rate=0.001, slippage_bps=5.0),
        timeframe="4h",
    )

    state_path.parent.mkdir(parents=True, exist_ok=True)
    if state_path.exists():
        state_path.unlink()
    store = StateStore(state_path)
    store.set_meta("started_at", df.index[0].isoformat())

    for t in result.trades:
        store.record_trade(
            ClosedTrade(
                symbol="BTC/USDT",
                entry_time=t.entry_time.to_pydatetime() if hasattr(t.entry_time, "to_pydatetime") else t.entry_time,
                exit_time=t.exit_time.to_pydatetime() if hasattr(t.exit_time, "to_pydatetime") else t.exit_time,
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                qty=t.qty,
                pnl=t.pnl,
                pnl_pct=t.pnl_pct,
                fees=t.fees,
                reason=t.reason,
            )
        )

    # Equity snapshots: sample every 6 bars to keep DB small.
    eq = result.equity_curve.iloc[::6]
    for ts, val in eq.items():
        store.record_equity(ts.to_pydatetime(), float(val))

    store.mark_heartbeat()

    console.print(
        f"[green]Seeded {state_path}[/green] with [bold]{len(result.trades)}[/bold] trades, "
        f"{len(eq)} equity snapshots."
    )
    console.print(
        f"View it with: [bold]crypto-bot dashboard --state {state_path}[/bold]"
    )


if __name__ == "__main__":
    app()
