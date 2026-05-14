import pytest

from crypto_bot.backtest.runner import CostModel
from crypto_bot.execution.base import OrderRequest, Side
from crypto_bot.execution.paper import PaperExecutor


def test_paper_executor_buy_then_sell_round_trip():
    cost = CostModel(fee_rate=0.001, slippage_bps=5.0)
    ex = PaperExecutor(cost, starting_balance=1000.0)

    buy = ex.submit(OrderRequest("BTC/USDT", Side.BUY, qty=0.01, reference_price=50_000))
    # Slippage 5bps => 50_000 * 1.0005 = 50_025; notional 500.25; fee 0.50025
    assert buy.price == pytest.approx(50_025, rel=1e-6)
    assert buy.fee == pytest.approx(0.50025, rel=1e-6)
    bal_after_buy = ex.get_free_quote_balance("USDT")
    assert bal_after_buy == pytest.approx(1000.0 - 500.25 - 0.50025, rel=1e-6)
    assert ex.balances["BTC"] == pytest.approx(0.01)

    sell = ex.submit(OrderRequest("BTC/USDT", Side.SELL, qty=0.01, reference_price=51_000))
    # Slippage: 51_000 * 0.9995 = 50_974.5; notional 509.745; fee 0.509745
    assert sell.price == pytest.approx(50_974.5, rel=1e-6)
    final_quote = ex.get_free_quote_balance("USDT")
    expected = 1000.0 - 500.25 - 0.50025 + 509.745 - 0.509745
    assert final_quote == pytest.approx(expected, rel=1e-6)
    assert ex.balances["BTC"] == pytest.approx(0.0)


def test_paper_executor_name():
    ex = PaperExecutor(CostModel(), 1000.0)
    assert ex.name() == "paper"


def test_paper_executor_idempotency():
    """Same client_order_id submitted twice yields the same Fill, no double-debit."""
    ex = PaperExecutor(CostModel(fee_rate=0.001, slippage_bps=5.0), starting_balance=1000.0)
    req = OrderRequest("BTC/USDT", Side.BUY, qty=0.01, reference_price=50_000,
                       client_order_id="bot-BTCUSDT-1700000000000-buy")

    first = ex.submit(req)
    bal_after_first = ex.get_free_quote_balance("USDT")
    btc_after_first = ex.balances["BTC"]

    second = ex.submit(req)  # same cid
    # The second submit must return the cached fill, not place a new order.
    assert second.order_id == first.order_id
    assert second.price == first.price
    assert ex.get_free_quote_balance("USDT") == pytest.approx(bal_after_first)
    assert ex.balances["BTC"] == pytest.approx(btc_after_first)


def test_paper_executor_distinct_cids_both_execute():
    """Different client_order_ids must both go through (no false-positive dedupe)."""
    ex = PaperExecutor(CostModel(), starting_balance=1000.0)
    req_a = OrderRequest("BTC/USDT", Side.BUY, qty=0.005, reference_price=50_000,
                         client_order_id="cid-a")
    req_b = OrderRequest("BTC/USDT", Side.BUY, qty=0.005, reference_price=50_000,
                         client_order_id="cid-b")
    ex.submit(req_a)
    ex.submit(req_b)
    assert ex.balances["BTC"] == pytest.approx(0.01)


def test_paper_executor_empty_cid_does_not_dedupe():
    """Without a cid, every submit is a fresh order."""
    ex = PaperExecutor(CostModel(), starting_balance=1000.0)
    req = OrderRequest("BTC/USDT", Side.BUY, qty=0.005, reference_price=50_000)
    ex.submit(req)
    ex.submit(req)  # no cid: two separate fills
    assert ex.balances["BTC"] == pytest.approx(0.01)
