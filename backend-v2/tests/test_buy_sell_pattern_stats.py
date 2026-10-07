import datetime as dt

import pytest

from db.models import BuySellPattern, BuySellPatternName, BuySellPatternStat, Ticker
from db.session import SessionLocal, init_db
from jobs.buy_sell_pattern_stats import compute_buy_sell_pattern_stats
from jobs.control import JobCancelled, JobControl

_NOW = dt.datetime(2026, 1, 1)


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(BuySellPatternStat).delete()
    session.query(BuySellPattern).delete()
    session.query(BuySellPatternName).delete()
    session.query(Ticker).delete()
    session.commit()
    session.close()
    yield


def _at(i: int) -> dt.datetime:
    """Price point i - day i // 2, at the open for even i and the close for odd i."""
    day = dt.date(2026, 1, 5) + dt.timedelta(days=i // 2)
    return dt.datetime.combine(day, dt.time(21, 0) if i % 2 else dt.time(9, 30))


def _seed(name: str, ticker: str, trades: list[tuple[int, float, int, float]]) -> None:
    """Each trade is (buy_point, buy_price, sell_point, sell_price) - see _at."""
    with SessionLocal() as session:
        if session.get(Ticker, ticker) is None:
            session.add(Ticker(ticker=ticker))
        pattern = session.query(BuySellPatternName).filter(BuySellPatternName.name == name).one_or_none()
        if pattern is None:
            pattern = BuySellPatternName(name=name, created_at=_NOW, updated_at=_NOW)
            session.add(pattern)
            session.flush()
        for buy_point, buy_price, sell_point, sell_price in trades:
            for buy_sell, point, price in (("buy", buy_point, buy_price), ("sell", sell_point, sell_price)):
                session.add(
                    BuySellPattern(
                        pattern_id=pattern.id,
                        ticker=ticker,
                        trade_datetime=_at(point),
                        buy_sell=buy_sell,
                        price=price,
                    )
                )
        session.commit()


def _stats() -> dict[tuple[str, str], BuySellPatternStat]:
    with SessionLocal() as session:
        return {(row.name, row.ticker): row for row in session.query(BuySellPatternStat)}


def test_computes_each_statistic_per_name_and_ticker():
    # Odd count, so medians are the middle value; (2, 3) is a same-day trade.
    _seed("p1", "AAA", [(0, 10, 1, 20), (2, 4, 3, 5), (4, 6, 7, 15)])
    _seed("p1", "BBB", [(0, 3, 1, 4), (2, 5, 3, 9)])
    _seed("p2", "AAA", [(0, 1, 1, 2)])

    with SessionLocal() as session:
        assert compute_buy_sell_pattern_stats(session) == 3
    stats = _stats()
    assert set(stats) == {("p1", "AAA"), ("p1", "BBB"), ("p2", "AAA")}

    aaa = stats[("p1", "AAA")]
    assert aaa.trades == 3
    # Per-trade profit %: 100, 25, 150.
    assert aaa.total_profit == pytest.approx(275)
    assert (aaa.first_trade_datetime, aaa.last_trade_datetime) == (_at(0), _at(7))
    assert aaa.avg_buy_price == pytest.approx(20 / 3)
    assert aaa.avg_sell_price == pytest.approx(40 / 3)
    assert aaa.avg_profit == pytest.approx(275 / 3)
    assert (aaa.buy_price_min, aaa.buy_price_max) == (4, 10)
    assert (aaa.sell_price_min, aaa.sell_price_max) == (5, 20)
    assert (aaa.buy_price_median, aaa.sell_price_median) == (6, 15)

    # Even count - median is the mean of the middle two.
    bbb = stats[("p1", "BBB")]
    assert (bbb.trades, bbb.buy_price_median, bbb.sell_price_median) == (2, 4, 6.5)
    # Per-trade profit %: 100/3, 80.
    assert bbb.avg_profit == pytest.approx((100 / 3 + 80) / 2)

    assert stats[("p2", "AAA")].trades == 1


def test_skips_trades_with_zero_buy_price():
    _seed("p1", "AAA", [(0, 0, 1, 5), (2, 4, 3, 5)])
    _seed("p1", "ZZZ", [(0, 0, 1, 5)])

    with SessionLocal() as session:
        assert compute_buy_sell_pattern_stats(session) == 1
    aaa = _stats()[("p1", "AAA")]
    assert (aaa.trades, aaa.buy_price_min) == (1, 4)
    assert aaa.total_profit == pytest.approx(25)
    assert (aaa.first_trade_datetime, aaa.last_trade_datetime) == (_at(2), _at(3))


def test_rerun_rebuilds_table_and_drops_removed_patterns():
    _seed("p1", "AAA", [(0, 10, 1, 20)])
    _seed("old", "AAA", [(0, 1, 1, 2)])
    with SessionLocal() as session:
        compute_buy_sell_pattern_stats(session)
        old_id = session.query(BuySellPatternName.id).filter(BuySellPatternName.name == "old").scalar()
        session.query(BuySellPattern).filter(BuySellPattern.pattern_id == old_id).delete()
        session.commit()
        assert compute_buy_sell_pattern_stats(session) == 1
    assert set(_stats()) == {("p1", "AAA")}


def test_cancel_leaves_existing_stats_untouched():
    _seed("p1", "AAA", [(0, 10, 1, 20)])
    with SessionLocal() as session:
        compute_buy_sell_pattern_stats(session)
    _seed("p2", "AAA", [(0, 1, 1, 2)])

    control = JobControl()
    control.request_cancel()
    with SessionLocal() as session, pytest.raises(JobCancelled):
        compute_buy_sell_pattern_stats(session, control=control)
    assert set(_stats()) == {("p1", "AAA")}


def test_empty_source_clears_stats():
    _seed("p1", "AAA", [(0, 10, 1, 20)])
    with SessionLocal() as session:
        compute_buy_sell_pattern_stats(session)
        session.query(BuySellPattern).delete()
        session.commit()
        assert compute_buy_sell_pattern_stats(session) == 0
    assert _stats() == {}
