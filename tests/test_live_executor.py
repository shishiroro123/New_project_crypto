"""Tests for the LiveExecutor against a stubbed ccxt client.

These cover the contract LiveExecutor depends on: amount precision rounding,
ccxt response shape (average / fee / fees / id), idempotency wiring through
the `clientOrderId` param, and balance accessor.
"""

from __future__ import annotations

from typing import Any

import pytest

from crypto_bot.execution.base import OrderRequest, Side
from crypto_bot.execution.live import LiveExecutor


class FakeCcxt:
    """Minimal ccxt.Exchange stand-in for LiveExecutor tests."""

    def __init__(self, *, balance: dict[str, dict[str, float]] | None = None,
                 fail_load_markets: bool = False) -> None:
        self._balance = balance or {"free": {"USDT": 1_000.0}}
        self._fail_load = fail_load_markets
        self.urls: dict[str, str] = {}
        self.create_order_calls: list[dict[str, Any]] = []
        self._fill_response: dict[str, Any] = {
            "id": "exch-1",
            "average": 100.0,
            "fees": [{"cost": 0.1, "currency": "USDT"}],
        }

    def load_markets(self) -> dict:
        if self._fail_load:
            raise RuntimeError("network down")
        return {}

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        # Round to 6 decimals like a typical spot venue.
        return f"{amount:.6f}".rstrip("0").rstrip(".") or "0"

    def create_order(self, symbol: str, type: str, side: str,  # noqa: A002
                     amount: float, params: dict | None = None) -> dict:
        self.create_order_calls.append(
            {"symbol": symbol, "type": type, "side": side,
             "amount": amount, "params": params or {}}
        )
        return dict(self._fill_response)

    def fetch_balance(self) -> dict:
        return self._balance


def test_live_executor_init_swallows_load_markets_error():
    """Construction must succeed even if load_markets fails (network might be flaky)."""
    LiveExecutor(FakeCcxt(fail_load_markets=True))  # no exception


def test_live_executor_submit_passes_clientOrderId():
    fake = FakeCcxt()
    ex = LiveExecutor(fake)
    cid = "bot-BTCUSDT-1700000000000-buy"
    fill = ex.submit(OrderRequest(
        symbol="BTC/USDT", side=Side.BUY, qty=0.01,
        reference_price=100.0, client_order_id=cid,
    ))
    assert fake.create_order_calls[0]["params"]["clientOrderId"] == cid
    assert fill.order_id == "exch-1"
    assert fill.price == pytest.approx(100.0)
    assert fill.fee == pytest.approx(0.1)


def test_live_executor_submit_no_cid_omits_param():
    fake = FakeCcxt()
    ex = LiveExecutor(fake)
    ex.submit(OrderRequest(
        symbol="BTC/USDT", side=Side.SELL, qty=0.01, reference_price=100.0,
    ))
    assert fake.create_order_calls[0]["params"] == {}


def test_live_executor_qty_rounded_to_zero_raises():
    fake = FakeCcxt()
    ex = LiveExecutor(fake)
    # Rounding 1e-9 to 6 decimals → 0; LiveExecutor must refuse to place a 0 order.
    with pytest.raises(ValueError):
        ex.submit(OrderRequest(
            symbol="BTC/USDT", side=Side.BUY, qty=1e-9, reference_price=100.0,
        ))


def test_live_executor_falls_back_to_reference_price_on_zero_average():
    fake = FakeCcxt()
    fake._fill_response = {"id": "x", "average": 0.0, "price": 0.0, "fees": []}
    ex = LiveExecutor(fake)
    fill = ex.submit(OrderRequest(
        symbol="BTC/USDT", side=Side.BUY, qty=0.01, reference_price=100.0,
    ))
    assert fill.price == pytest.approx(100.0)


def test_live_executor_uses_fee_singular_when_fees_empty():
    fake = FakeCcxt()
    fake._fill_response = {
        "id": "x", "average": 100.0, "fees": [], "fee": {"cost": 0.25, "currency": "USDT"},
    }
    ex = LiveExecutor(fake)
    fill = ex.submit(OrderRequest(
        symbol="BTC/USDT", side=Side.BUY, qty=0.01, reference_price=100.0,
    ))
    assert fill.fee == pytest.approx(0.25)


def test_live_executor_get_free_quote_balance():
    fake = FakeCcxt(balance={"free": {"USDT": 750.5, "BTC": 0.01}})
    ex = LiveExecutor(fake)
    assert ex.get_free_quote_balance("USDT") == pytest.approx(750.5)
    assert ex.get_free_quote_balance("EUR") == pytest.approx(0.0)
