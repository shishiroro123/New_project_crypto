"""SQLite storage for the watcher.

Keeps:
- seen_signatures: dedup table so we don't re-alert on the same tx
- detected_buys:   the actual alerts, with all the metrics we collected
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_signatures (
    signature   TEXT PRIMARY KEY,
    wallet      TEXT NOT NULL,
    detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS detected_buys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signature       TEXT UNIQUE NOT NULL,
    wallet          TEXT NOT NULL,
    wallet_name     TEXT,
    token_mint      TEXT NOT NULL,
    token_symbol    TEXT,
    quote_mint      TEXT NOT NULL,
    amount_in       REAL,
    amount_out      REAL,
    amount_in_usd   REAL,
    tx_timestamp    TEXT NOT NULL,
    detected_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_seen_wallet ON seen_signatures(wallet);
CREATE INDEX IF NOT EXISTS idx_buys_wallet ON detected_buys(wallet);
CREATE INDEX IF NOT EXISTS idx_buys_token  ON detected_buys(token_mint);
"""


class Storage:
    def __init__(self, path: Path) -> None:
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

    # --- dedup ---------------------------------------------------------------

    def has_seen(self, signature: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM seen_signatures WHERE signature = ?",
                (signature,),
            ).fetchone()
            return row is not None

    def mark_seen(self, signature: str, wallet: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO seen_signatures (signature, wallet, detected_at) "
                "VALUES (?, ?, ?)",
                (signature, wallet, _now_iso()),
            )

    # --- buys ----------------------------------------------------------------

    def record_buy(
        self,
        signature: str,
        wallet: str,
        wallet_name: str | None,
        token_mint: str,
        token_symbol: str | None,
        quote_mint: str,
        amount_in: float | None,
        amount_out: float | None,
        amount_in_usd: float | None,
        tx_timestamp: str,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT OR IGNORE INTO detected_buys (
                    signature, wallet, wallet_name, token_mint, token_symbol,
                    quote_mint, amount_in, amount_out, amount_in_usd,
                    tx_timestamp, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signature,
                    wallet,
                    wallet_name,
                    token_mint,
                    token_symbol,
                    quote_mint,
                    amount_in,
                    amount_out,
                    amount_in_usd,
                    tx_timestamp,
                    _now_iso(),
                ),
            )

    def stats(self) -> dict[str, int]:
        with self._conn() as c:
            seen = c.execute("SELECT COUNT(*) FROM seen_signatures").fetchone()[0]
            buys = c.execute("SELECT COUNT(*) FROM detected_buys").fetchone()[0]
            return {"seen_signatures": seen, "detected_buys": buys}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
