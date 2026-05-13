from datetime import UTC, datetime

from crypto_bot.state import ClosedTrade, OpenPosition, StateStore


def test_position_upsert_and_close(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    assert store.get_position("BTC/USDT") is None

    pos = OpenPosition(
        symbol="BTC/USDT",
        qty=0.01,
        entry_price=50_000,
        stop_price=48_000,
        entry_time=datetime(2024, 1, 1, tzinfo=UTC),
        fees_paid=0.5,
    )
    store.upsert_position(pos)
    got = store.get_position("BTC/USDT")
    assert got is not None
    assert got.qty == 0.01
    assert got.stop_price == 48_000

    # Upsert: change stop, ensure single row.
    pos2 = OpenPosition(**{**pos.__dict__, "stop_price": 49_000})
    store.upsert_position(pos2)
    assert len(store.all_positions()) == 1
    assert store.get_position("BTC/USDT").stop_price == 49_000  # type: ignore[union-attr]

    store.close_position("BTC/USDT")
    assert store.get_position("BTC/USDT") is None


def test_trade_record_and_recent(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    trade = ClosedTrade(
        symbol="ETH/USDT",
        entry_time=datetime(2024, 1, 1, tzinfo=UTC),
        exit_time=datetime(2024, 1, 2, tzinfo=UTC),
        entry_price=2000,
        exit_price=2100,
        qty=0.5,
        pnl=50,
        pnl_pct=0.05,
        fees=2.0,
        reason="signal",
    )
    store.record_trade(trade)
    recent = store.recent_trades(10)
    assert len(recent) == 1
    assert recent[0].pnl == 50


def test_equity_snapshots(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    t1 = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    t2 = datetime(2024, 1, 1, 4, 0, tzinfo=UTC)
    store.record_equity(t1, 500.0)
    store.record_equity(t2, 510.0)
    latest = store.latest_equity()
    assert latest is not None
    assert latest[1] == 510.0


def test_meta_kv(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    assert store.get_meta("foo") is None
    store.set_meta("foo", "bar")
    assert store.get_meta("foo") == "bar"
    store.set_meta("foo", "baz")
    assert store.get_meta("foo") == "baz"
