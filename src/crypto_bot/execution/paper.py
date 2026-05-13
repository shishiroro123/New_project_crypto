"""Paper trading executor.

Simulates fills with the same cost model used by the backtester so that
backtest and paper results are consistent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from crypto_bot.backtest.runner import CostModel
from crypto_bot.execution.base import ExecutorBase, Fill, OrderRequest, Side
from crypto_bot.logging_setup import get_logger

log = get_logger(__name__)


class PaperExecutor(ExecutorBase):
    def __init__(self, cost: CostModel, starting_balance: float, quote: str = "USDT") -> None:
        self._cost = cost
        self._balance: dict[str, float] = {quote: starting_balance}
        self._quote = quote

    def submit(self, order: OrderRequest) -> Fill:
        if order.side == Side.BUY:
            price = self._cost.buy_price(order.reference_price)
        else:
            price = self._cost.sell_price(order.reference_price)

        notional = order.qty * price
        fee = notional * self._cost.fee_rate

        base = order.symbol.split("/")[0]
        if order.side == Side.BUY:
            cost = notional + fee
            self._balance[self._quote] = self._balance.get(self._quote, 0.0) - cost
            self._balance[base] = self._balance.get(base, 0.0) + order.qty
        else:
            self._balance[base] = self._balance.get(base, 0.0) - order.qty
            self._balance[self._quote] = (
                self._balance.get(self._quote, 0.0) + notional - fee
            )

        fill = Fill(
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=price,
            fee=fee,
            timestamp=datetime.now(UTC),
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
        )
        log.info(
            "paper.fill",
            symbol=order.symbol,
            side=order.side.value,
            qty=order.qty,
            price=price,
            fee=fee,
            balance=self._balance.get(self._quote, 0.0),
        )
        return fill

    def get_free_quote_balance(self, quote: str = "USDT") -> float:
        return self._balance.get(quote, 0.0)

    def name(self) -> str:
        return "paper"

    @property
    def balances(self) -> dict[str, float]:
        return dict(self._balance)
