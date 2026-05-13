import pytest

from crypto_bot.risk.sizing import SizingParams, position_size


def test_position_size_risk_per_trade():
    # 500 equity, 1% risk = 5$ risk. Stop is 5$ below entry => qty == 1.0.
    qty = position_size(500.0, 100.0, 95.0, SizingParams(risk_per_trade=0.01))
    assert qty == pytest.approx(1.0)


def test_position_size_caps_at_max_pct():
    # 1% risk on $500 = $5; entry $100 stop $99.99 => raw qty huge.
    # Cap = 50% * 500 / 100 = 2.5 units.
    qty = position_size(500.0, 100.0, 99.99, SizingParams(risk_per_trade=0.01, max_position_pct=0.5))
    assert qty == pytest.approx(2.5)


def test_position_size_zero_when_stop_above_entry():
    assert position_size(500.0, 100.0, 101.0, SizingParams()) == 0.0


def test_position_size_zero_when_equity_nonpositive():
    assert position_size(0.0, 100.0, 95.0, SizingParams()) == 0.0
    assert position_size(-10.0, 100.0, 95.0, SizingParams()) == 0.0
