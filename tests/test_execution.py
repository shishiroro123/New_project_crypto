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
