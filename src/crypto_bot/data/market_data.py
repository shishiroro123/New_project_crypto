"""Historical OHLCV fetcher with on-disk parquet cache.

Strategy:
- For each (symbol, timeframe), keep one parquet file under <data_dir>/ohlcv/.
- On request, read the cache, fetch only the missing tail from the exchange,
  append, dedupe, and rewrite. Crash-safe via temp file rename.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import ccxt
import pandas as pd

from crypto_bot.data.exchange import fetch_ohlcv
from crypto_bot.logging_setup import get_logger

log = get_logger(__name__)

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def _timeframe_to_ms(tf: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    unit = tf[-1]
    if unit not in units:
        raise ValueError(f"unsupported timeframe: {tf}")
    return int(tf[:-1]) * units[unit]


def _symbol_to_filename(symbol: str, timeframe: str) -> str:
    safe = symbol.replace("/", "-")
    return f"{safe}_{timeframe}.parquet"


def cache_path(data_dir: Path | str, symbol: str, timeframe: str) -> Path:
    base = Path(data_dir) / "ohlcv"
    base.mkdir(parents=True, exist_ok=True)
    return base / _symbol_to_filename(symbol, timeframe)


def load_cached(data_dir: Path | str, symbol: str, timeframe: str) -> pd.DataFrame:
    path = cache_path(data_dir, symbol, timeframe)
    if not path.exists():
        return pd.DataFrame(columns=OHLCV_COLUMNS).set_index("timestamp")
    df = pd.read_parquet(path)
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    return df


def save_cache(df: pd.DataFrame, data_dir: Path | str, symbol: str, timeframe: str) -> None:
    path = cache_path(data_dir, symbol, timeframe)
    tmp = path.with_suffix(".parquet.tmp")
    df.reset_index().to_parquet(tmp, index=False)
    tmp.replace(path)


def fetch_history(
    client: ccxt.binance,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime | None = None,
    data_dir: Path | str = "./data",
) -> pd.DataFrame:
    """Fetch full OHLCV history [start, end), using cache when possible."""
    end = end or datetime.now(UTC)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)

    cached = load_cached(data_dir, symbol, timeframe)
    tf_ms = _timeframe_to_ms(timeframe)

    if not cached.empty:
        cached_end = cached.index.max()
        fetch_since = max(int(cached_end.timestamp() * 1000) + tf_ms, int(start.timestamp() * 1000))
    else:
        fetch_since = int(start.timestamp() * 1000)

    end_ms = int(end.timestamp() * 1000)
    rows: list[list[float]] = []

    while fetch_since < end_ms:
        batch = fetch_ohlcv(client, symbol, timeframe, since_ms=fetch_since, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        last_ts = batch[-1][0]
        # Advance even if exchange returns duplicates (defensive).
        fetch_since = last_ts + tf_ms
        log.debug("ohlcv.batch", symbol=symbol, tf=timeframe, n=len(batch), last=last_ts)

    if rows:
        fresh = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
        fresh["timestamp"] = pd.to_datetime(fresh["timestamp"], unit="ms", utc=True)
        fresh = fresh.set_index("timestamp")
        merged = pd.concat([cached, fresh])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        save_cache(merged, data_dir, symbol, timeframe)
    else:
        merged = cached

    if merged.empty:
        return merged
    mask = (merged.index >= pd.Timestamp(start)) & (merged.index < pd.Timestamp(end))
    return merged.loc[mask].copy()
