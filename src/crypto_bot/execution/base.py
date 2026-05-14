"""Abstract executor + value objects.

Two implementations:
- PaperExecutor: simulates fills against a last-known price (used for paper trading
  and for live mode when EXECUTION_MODE=paper).
- LiveExecutor: submits market orders to Binance via ccxt.

Both implement the same minimal interface so the live runner is agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    qty: float
    # Reference mid price the strategy used for sizing — paper executor will fill
    # near this with slippage; live executor ignores it (real fills come from
    # the exchange).
    reference_price: float
    # Deterministic client-side ID for idempotency. Same cid on retry returns
    # the cached fill in paper mode, and reuses the exchange-side order in
    # live mode (Binance: clientOrderId). Empty disables idempotency.
    client_order_id: str = ""


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float
    timestamp: datetime
    order_id: str = ""


class ExecutorBase(ABC):
    @abstractmethod
    def submit(self, order: OrderRequest) -> Fill:
        ...

    @abstractmethod
    def get_free_quote_balance(self, quote: str = "USDT") -> float:
        ...

    @abstractmethod
    def name(self) -> str:
        ...
