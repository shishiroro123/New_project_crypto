"""Lightweight SQLite state store.

Tables:
- positions: at most one row per symbol while a position is open
- trades:    one row per closed trade (audit log)
- equity:    periodic snapshots of mark-to-market equity
- bot_meta:  key/value (last_run, started_at, ...)

We avoid ORMs on purpose. The schema is tiny and stable.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    symbol        TEXT PRIMARY KEY,
    qty           REAL NOT NULL,
    entry_price   REAL NOT NULL,
    stop_price    REAL NOT NULL,
    entry_time    TEXT NOT NULL,
    fees_paid     REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    entry_time    TEXT NOT NULL,
    exit_time     TEXT NOT NULL,
    entry_price   REAL NOT NULL,
    exit_price    REAL NOT NULL,
    qty           REAL NOT NULL,
    pnl           REAL NOT NULL,
    pnl_pct       REAL NOT NULL,
    fees          REAL NOT NULL,
    reason        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time);

CREATE TABLE IF NOT EXISTS equity (
    ts       TEXT PRIMARY KEY,
    equity   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_meta (
    key      TEXT PRIMARY KEY,
    value    TEXT NOT NULL
);
"""


@dataclass
class OpenPosition:
    symbol: str
    qty: float
    entry_price: float
    stop_price: float
    entry_time: datetime
    fees_paid: float = 0.0


@dataclass
class ClosedTrade:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    fees: float
    reason: str


class StateStore:
    def __init__(self, path: Path | str = "data/state.sqlite") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    # --- positions ----------------------------------------------------------

    def upsert_position(self, pos: OpenPosition) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO positions(symbol, qty, entry_price, stop_price, entry_time, fees_paid)
                VALUES (:symbol, :qty, :entry_price, :stop_price, :entry_time, :fees_paid)
                ON CONFLICT(symbol) DO UPDATE SET
                  qty=excluded.qty,
                  entry_price=excluded.entry_price,
                  stop_price=excluded.stop_price,
                  entry_time=excluded.entry_time,
                  fees_paid=excluded.fees_paid
                """,
                {**asdict(pos), "entry_time": pos.entry_time.isoformat()},
            )

    def get_position(self, symbol: str) -> OpenPosition | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
        if not row:
            return None
        return OpenPosition(
            symbol=row["symbol"],
            qty=row["qty"],
            entry_price=row["entry_price"],
            stop_price=row["stop_price"],
            entry_time=datetime.fromisoformat(row["entry_time"]),
            fees_paid=row["fees_paid"],
        )

    def all_positions(self) -> list[OpenPosition]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM positions").fetchall()
        return [
            OpenPosition(
                symbol=r["symbol"],
                qty=r["qty"],
                entry_price=r["entry_price"],
                stop_price=r["stop_price"],
                entry_time=datetime.fromisoformat(r["entry_time"]),
                fees_paid=r["fees_paid"],
            )
            for r in rows
        ]

    def close_position(self, symbol: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM positions WHERE symbol=?", (symbol,))

    # --- trades -------------------------------------------------------------

    def record_trade(self, trade: ClosedTrade) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO trades(symbol, entry_time, exit_time, entry_price, exit_price,
                                   qty, pnl, pnl_pct, fees, reason)
                VALUES (:symbol, :entry_time, :exit_time, :entry_price, :exit_price,
                        :qty, :pnl, :pnl_pct, :fees, :reason)
                """,
                {
                    **asdict(trade),
                    "entry_time": trade.entry_time.isoformat(),
                    "exit_time": trade.exit_time.isoformat(),
                },
            )

    def recent_trades(self, limit: int = 50) -> list[ClosedTrade]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            ClosedTrade(
                symbol=r["symbol"],
                entry_time=datetime.fromisoformat(r["entry_time"]),
                exit_time=datetime.fromisoformat(r["exit_time"]),
                entry_price=r["entry_price"],
                exit_price=r["exit_price"],
                qty=r["qty"],
                pnl=r["pnl"],
                pnl_pct=r["pnl_pct"],
                fees=r["fees"],
                reason=r["reason"],
            )
            for r in rows
        ]

    # --- equity -------------------------------------------------------------

    def record_equity(self, ts: datetime, equity: float) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO equity(ts, equity) VALUES(?, ?)",
                (ts.isoformat(), equity),
            )

    def latest_equity(self) -> tuple[datetime, float] | None:
        with self._conn() as c:
            row = c.execute("SELECT ts, equity FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        if not row:
            return None
        return datetime.fromisoformat(row["ts"]), row["equity"]

    # --- meta ---------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO bot_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM bot_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def mark_heartbeat(self) -> None:
        self.set_meta("last_heartbeat", datetime.now(UTC).isoformat())
