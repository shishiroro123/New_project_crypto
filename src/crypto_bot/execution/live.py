"""Live executor (Binance via ccxt).

Used for both testnet and real-money execution: same code path, distinguished
only by the `testnet` flag on the underlying ccxt client.

Submits MARKET orders. Slippage and fees come from the actual exchange,
NOT the CostModel (the CostModel is only used by paper trading and backtests).
"""

from __future__ import annotations

from datetime import UTC, datetime

import ccxt

from crypto_bot.execution.base import ExecutorBase, Fill, OrderRequest, Side
from crypto_bot.logging_setup import get_logger

log = get_logger(__name__)


class LiveExecutor(ExecutorBase):
    def __init__(self, client: "ccxt.Exchange") -> None:
        self._client = client
        try:
            self._client.load_markets()
        except Exception as exc:  # noqa: BLE001
            log.warning("live.load_markets_failed", error=str(exc))

    def submit(self, order: OrderRequest) -> Fill:
        # Snap qty to exchange precision; if amount falls below min, raise.
        try:
            qty = float(self._client.amount_to_precision(order.symbol, order.qty))
        except Exception:  # noqa: BLE001
            qty = order.qty
        if qty <= 0:
            raise ValueError(f"qty rounded to zero for {order.symbol}: {order.qty}")

        side = "buy" if order.side == Side.BUY else "sell"
        log.info("live.submit", symbol=order.symbol, side=side, qty=qty,
                 cid=order.client_order_id or None)

        # Idempotency: passing the same clientOrderId twice on Binance returns
        # the original order rather than creating a duplicate. ccxt normalises
        # this via the `clientOrderId` param across supported venues.
        params: dict = {}
        if order.client_order_id:
            params["clientOrderId"] = order.client_order_id

        resp = self._client.create_order(
            symbol=order.symbol,
            type="market",
            side=side,
            amount=qty,
            params=params,
        )

        # ccxt normalises avg fill price into 'average' (fallback to 'price' or last fill).
        price = (
            float(resp.get("average") or resp.get("price") or 0.0)
            or order.reference_price
        )
        fee = 0.0
        fees_field = resp.get("fees") or []
        for f in fees_field:
            if "cost" in f:
                fee += float(f["cost"])
        if not fees_field:
            single = resp.get("fee") or {}
            if "cost" in single:
                fee = float(single["cost"])

        fill = Fill(
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            price=price,
            fee=fee,
            timestamp=datetime.now(UTC),
            order_id=str(resp.get("id", "")),
        )
        log.info(
            "live.filled",
            symbol=order.symbol,
            side=side,
            qty=qty,
            price=price,
            fee=fee,
            order_id=fill.order_id,
        )
        return fill

    def get_free_quote_balance(self, quote: str = "USDT") -> float:
        balance = self._client.fetch_balance()
        free = balance.get("free", {}) or {}
        return float(free.get(quote, 0.0))

    def name(self) -> str:
        return "live(testnet)" if getattr(self._client, "urls", {}).get("test") else "live"
