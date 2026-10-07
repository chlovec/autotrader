import datetime as dt
import random

import pytest
from fastapi import HTTPException

from db.models import BuySellPattern, BuySellPatternName, BuySellPatternStat, JobConfig, OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.buy_sell_pattern import (
    DailyBar,
    PatternNameConflict,
    Trade,
    close_datetime,
    compute_buy_sell_pattern,
    default_pattern_name,
    max_profit_trades_unlimited,
    open_datetime,
    pattern_name_exists,
    resolve_pattern_name,
    validate_date_range,
)
from jobs.buy_sell_pattern_stats import compute_buy_sell_pattern_stats
from jobs.registry import BUY_SELL_PATTERN_JOB


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(BuySellPatternStat).delete()
    session.query(BuySellPattern).delete()
    session.query(BuySellPatternName).delete()
    session.query(OhlcBar).delete()
    session.query(JobConfig).delete()
    session.query(Ticker).delete()
    session.commit()
    session.close()
    yield


_START = dt.date(2026, 1, 5)


def _day(i: int) -> dt.date:
    return _START + dt.timedelta(days=i)


def _open(i: int) -> dt.datetime:
    return open_datetime(_day(i))


def _close(i: int) -> dt.datetime:
    return close_datetime(_day(i))


def _bars(*oc: tuple[float, float]) -> list[DailyBar]:
    return [DailyBar(_day(i), open_, close) for i, (open_, close) in enumerate(oc)]


def _total(trades: list[Trade]) -> float:
    return sum(t.profit for t in trades)


def _brute_force(bars: list[DailyBar]) -> float:
    """Exhaustive search over the same rules as max_profit_trades_unlimited, on the
    open/close price points: from point i with nothing held, either skip point i, or buy
    at point i and sell at some later point j, then continue from point j + 1."""
    points = [price for bar in bars for price in (bar.open, bar.close)]
    n = len(points)
    best = [0.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        best[i] = best[i + 1]
        for j in range(i + 1, n):
            best[i] = max(best[i], points[j] - points[i] + best[j + 1])
    return best[0]


def test_empty_and_single_flat_day_have_no_trades():
    assert max_profit_trades_unlimited([]) == []
    # close == open - no up-move to trade.
    assert max_profit_trades_unlimited(_bars((10, 10))) == []


def test_same_day_trade_when_close_above_open():
    trades = max_profit_trades_unlimited(_bars((10, 12)))
    assert trades == [Trade(_open(0), 10, _close(0), 12)]


def test_rising_run_across_days_is_one_trade():
    # Points 10, 10, 11, 13 - buys at the start of the run, sells at its end (13 - 10 = 3
    # beats day 1's own same-day 13 - 11 = 2). A tie between day 0's open and close buys
    # at the later point, the close.
    trades = max_profit_trades_unlimited(_bars((10, 10), (11, 13)))
    assert trades == [Trade(_close(0), 10, _close(1), 13)]


def test_holds_across_days_when_that_beats_separate_trades():
    # Separate same-day trades: (10.5-10) + (21-20) = 1.5. Holding: 21 - 10 = 11.
    trades = max_profit_trades_unlimited(_bars((10, 10.5), (20, 21)))
    assert trades == [Trade(_open(0), 10, _close(1), 21)]


def test_buys_at_a_close():
    # Points 10, 5, 8, 12 - the low point is day 0's close.
    trades = max_profit_trades_unlimited(_bars((10, 5), (8, 12)))
    assert trades == [Trade(_close(0), 5, _close(1), 12)]


def test_sells_at_an_open():
    # Points 10, 12, 20, 5 - the high point is day 1's open.
    trades = max_profit_trades_unlimited(_bars((10, 12), (20, 5)))
    assert trades == [Trade(_open(0), 10, _open(1), 20)]


def test_sells_at_an_open_and_buys_back_at_that_days_close():
    # Points 10, 12, 20, 5, 9, 15 - sells at day 1's open, rebuys at day 1's close.
    trades = max_profit_trades_unlimited(_bars((10, 12), (20, 5), (9, 15)))
    assert trades == [Trade(_open(0), 10, _open(1), 20), Trade(_close(1), 5, _close(2), 15)]


def test_multiple_non_overlapping_trades():
    bars = _bars((10, 10), (15, 20), (5, 5), (8, 12))
    trades = max_profit_trades_unlimited(bars)
    assert trades == [Trade(_close(0), 10, _close(1), 20), Trade(_close(2), 5, _close(3), 12)]
    for earlier, later in zip(trades, trades[1:]):
        assert later.buy_datetime > earlier.sell_datetime


def test_falling_prices_with_closes_at_opens_have_no_trades():
    assert max_profit_trades_unlimited(_bars((10, 10), (9, 9), (8, 8))) == []


def test_matches_brute_force_on_random_series():
    rng = random.Random(42)
    for _ in range(300):
        bars = []
        price = 50.0
        for i in range(rng.randint(1, 12)):
            price = max(1.0, price + rng.uniform(-5, 5))
            open_ = price
            close = rng.choice([open_, max(0.5, open_ + rng.uniform(-3, 3))])
            bars.append(DailyBar(_day(i), open_, close))
        trades = max_profit_trades_unlimited(bars)
        assert _total(trades) == pytest.approx(_brute_force(bars))
        for trade in trades:
            assert trade.profit > 0
            assert trade.sell_datetime > trade.buy_datetime
        for earlier, later in zip(trades, trades[1:]):
            # A sell at a day's open can be followed by a buy at that day's close.
            assert later.buy_datetime > earlier.sell_datetime


def test_resolve_pattern_name():
    start, end = dt.date(2026, 1, 1), dt.date(2026, 3, 31)
    assert default_pattern_name(start, end) == "2026-01-01_2026-03-31"
    assert resolve_pattern_name("mine", start, end, "manual") == "mine"
    assert resolve_pattern_name("  ", start, end, "manual") == "2026-01-01_2026-03-31"
    assert resolve_pattern_name(None, start, end, "manual") == "2026-01-01_2026-03-31"
    # Auto runs never use the override.
    assert resolve_pattern_name("mine", start, end, "auto") == "2026-01-01_2026-03-31"
    # A missing date gets a placeholder.
    assert default_pattern_name(start, None) == "2026-01-01_latest"
    assert default_pattern_name(None, end) == "earliest_2026-03-31"
    assert resolve_pattern_name(None, None, None, "auto") == "earliest_latest"


def test_validate_date_range():
    # Either or both dates may be omitted.
    assert validate_date_range(None, dt.date(2026, 1, 1)) == (None, dt.date(2026, 1, 1))
    assert validate_date_range(dt.date(2026, 1, 1), None) == (dt.date(2026, 1, 1), None)
    assert validate_date_range(None, None) == (None, None)
    with pytest.raises(ValueError):
        validate_date_range(dt.date(2026, 1, 2), dt.date(2026, 1, 1))
    assert validate_date_range(dt.date(2026, 1, 1), dt.date(2026, 1, 1)) == (dt.date(2026, 1, 1), dt.date(2026, 1, 1))


# compute_buy_sell_pattern reads the tickers_daily_bars_min_60_days_from_latest
# view, which only returns tickers with a bar on the latest day and a full 60 most
# recent daily bars - so _seed_bars pads every ticker out to this many days.
_VIEW_MIN_BARS = 60


def _seed_bars(ticker: str, oc: list[tuple[float, float]], ticker_type: str = "CS") -> None:
    """Seeds `oc` (open, close) on days 0, 1, ..., then flat (open == close) filler bars
    through day _VIEW_MIN_BARS - 1 so the ticker shows up in the view. Tests keep their
    date ranges before the filler."""
    prices = list(oc) + [(1.0, 1.0)] * (_VIEW_MIN_BARS - len(oc))
    with SessionLocal() as session:
        session.add(Ticker(ticker=ticker, type=ticker_type))
        for i, (open_, close) in enumerate(prices):
            session.add(
                OhlcBar(
                    ticker=ticker,
                    multiplier=1,
                    timespan="day",
                    timestamp=dt.datetime.combine(_day(i), dt.time.min),
                    open=open_,
                    low=min(open_, close),
                    high=max(open_, close),
                    close=close,
                )
            )
        session.commit()


def _rows(name: str) -> list[tuple[str, dt.datetime, str, float]]:
    with SessionLocal() as session:
        return [
            (r.ticker, r.trade_datetime, r.buy_sell, r.price)
            for r in session.query(BuySellPattern)
            .join(BuySellPatternName, BuySellPatternName.id == BuySellPattern.pattern_id)
            .filter(BuySellPatternName.name == name)
            .order_by(BuySellPattern.ticker, BuySellPattern.trade_datetime)
        ]


def test_compute_stores_buy_and_sell_rows_within_range():
    _seed_bars("AAA", [(10, 10), (15, 20), (5, 5), (8, 12), (1, 100)])
    with SessionLocal() as session:
        # End date excludes day 4's huge close.
        result = compute_buy_sell_pattern(session, _day(0), _day(3), "p1")
    assert (result.tickers, result.trades, result.replaced) == (1, 2, False)
    assert _rows("p1") == [
        ("AAA", _close(0), "buy", 10),
        ("AAA", _close(1), "sell", 20),
        ("AAA", _close(2), "buy", 5),
        ("AAA", _close(3), "sell", 12),
    ]


def test_compute_open_ended_ranges():
    _seed_bars("AAA", [(10, 10), (15, 20), (5, 5), (8, 12), (1, 100)])
    with SessionLocal() as session:
        start_only = compute_buy_sell_pattern(session, _day(2), None, "start_only")
        end_only = compute_buy_sell_pattern(session, None, _day(1), "end_only")
        unbounded = compute_buy_sell_pattern(session, None, None, "unbounded")
    assert (start_only.trades, end_only.trades, unbounded.trades) == (2, 1, 3)
    assert _rows("start_only") == [
        ("AAA", _close(2), "buy", 5),
        ("AAA", _close(3), "sell", 12),
        ("AAA", _open(4), "buy", 1),
        ("AAA", _close(4), "sell", 100),
    ]
    assert _rows("end_only") == [("AAA", _close(0), "buy", 10), ("AAA", _close(1), "sell", 20)]
    assert [(at, side) for _, at, side, _ in _rows("unbounded")] == [
        (_close(0), "buy"),
        (_close(1), "sell"),
        (_close(2), "buy"),
        (_close(3), "sell"),
        (_open(4), "buy"),
        (_close(4), "sell"),
    ]


def test_compute_same_day_trade_stores_buy_at_open_and_sell_at_close():
    _seed_bars("AAA", [(10, 12)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(0), "p1")
    assert _rows("p1") == [("AAA", _open(0), "buy", 10), ("AAA", _close(0), "sell", 12)]


def test_compute_respects_ticker_selection():
    _seed_bars("AAA", [(10, 12)])
    _seed_bars("BBB", [(10, 12)], ticker_type="ETF")
    with SessionLocal() as session:
        result = compute_buy_sell_pattern(session, _day(0), _day(0), "p1", ticker_types=["ETF"])
    assert result.tickers == 1
    assert {row[0] for row in _rows("p1")} == {"BBB"}


def test_compute_name_conflict_fails_without_replace_and_keeps_rows():
    _seed_bars("AAA", [(10, 12)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(0), "p1")
    before = _rows("p1")
    with SessionLocal() as session, pytest.raises(PatternNameConflict):
        compute_buy_sell_pattern(session, _day(0), _day(0), "p1")
    assert _rows("p1") == before


def test_compute_replace_rewrites_all_rows_for_name():
    _seed_bars("AAA", [(10, 12)])
    _seed_bars("BBB", [(10, 12)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(0), "p1")
        compute_buy_sell_pattern(session, _day(0), _day(0), "other")
        result = compute_buy_sell_pattern(session, _day(0), _day(0), "p1", tickers=["AAA"], replace=True)
    assert result.replaced
    # BBB's old p1 rows are gone too - replace swaps the whole name's output.
    assert {row[0] for row in _rows("p1")} == {"AAA"}
    assert {row[0] for row in _rows("other")} == {"AAA", "BBB"}


class _FailOnBatch:
    """Stands in for JobControl - raises at the start of the `fail_at`th batch (1-based)."""

    def __init__(self, fail_at: int) -> None:
        self.fail_at = fail_at
        self.calls = 0

    def checkpoint_sync(self) -> None:
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("boom")


def test_compute_error_after_earlier_batches_writes_nothing():
    _seed_bars("AAA", [(10, 12)])
    _seed_bars("BBB", [(10, 12)])
    with SessionLocal() as session, pytest.raises(RuntimeError):
        compute_buy_sell_pattern(session, _day(0), _day(0), "p1", batch_size=1, control=_FailOnBatch(2))
    assert _rows("p1") == []
    # The staging table is cleaned up, so the same name can run again straight away.
    with SessionLocal() as session:
        result = compute_buy_sell_pattern(session, _day(0), _day(0), "p1", batch_size=1)
    assert result.trades == 2
    assert {row[0] for row in _rows("p1")} == {"AAA", "BBB"}


def test_compute_replace_error_keeps_old_rows():
    _seed_bars("AAA", [(10, 12)])
    _seed_bars("BBB", [(10, 12)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(0), "p1")
    before = _rows("p1")
    with SessionLocal() as session, pytest.raises(RuntimeError):
        compute_buy_sell_pattern(
            session, _day(0), _day(0), "p1", replace=True, batch_size=1, control=_FailOnBatch(2)
        )
    assert _rows("p1") == before


def test_trigger_job_warns_on_name_conflict_and_accepts_replace():
    from app.main import JobRunOverridesIn, trigger_job

    _seed_bars("AAA", [(10, 12)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(0), default_pattern_name(_day(0), _day(0)))

    body = JobRunOverridesIn(buy_sell_pattern_start_date=_day(0).isoformat(), buy_sell_pattern_end_date=_day(0).isoformat())
    with pytest.raises(HTTPException) as exc_info:
        trigger_job(BUY_SELL_PATTERN_JOB, body)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "name_conflict"
    assert exc_info.value.detail["name"] == "2026-01-05_2026-01-05"

    # A unique name goes through.
    assert trigger_job(BUY_SELL_PATTERN_JOB, body.model_copy(update={"buy_sell_pattern_name": "fresh"})) == {
        "status": "started"
    }


def test_trigger_job_replace_flag_bypasses_conflict():
    from app.main import JobRunOverridesIn, trigger_job

    _seed_bars("AAA", [(10, 12)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(0), "taken")
    body = JobRunOverridesIn(
        buy_sell_pattern_start_date=_day(0).isoformat(),
        buy_sell_pattern_end_date=_day(0).isoformat(),
        buy_sell_pattern_name="taken",
        buy_sell_pattern_replace=True,
    )
    assert trigger_job(BUY_SELL_PATTERN_JOB, body) == {"status": "started"}
    with SessionLocal() as session:
        config = session.get(JobConfig, BUY_SELL_PATTERN_JOB)
        assert '"buy_sell_pattern_replace": true' in config.run_overrides


def test_trigger_job_allows_open_ended_date_range():
    from app.main import JobRunOverridesIn, trigger_job

    _seed_bars("AAA", [(10, 12)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, None, None, "earliest_latest")
    with pytest.raises(HTTPException) as exc_info:
        trigger_job(BUY_SELL_PATTERN_JOB, JobRunOverridesIn())
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["name"] == "earliest_latest"
    body = JobRunOverridesIn(buy_sell_pattern_start_date=_day(0).isoformat())
    assert trigger_job(BUY_SELL_PATTERN_JOB, body) == {"status": "started"}


def test_runs_endpoint_lists_names_with_spans_newest_first():
    from app.main import buy_sell_pattern_runs

    _seed_bars("AAA", [(10, 10), (15, 20), (5, 5), (8, 12)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(1), "early")
        compute_buy_sell_pattern(session, _day(0), _day(3), "full")
    assert buy_sell_pattern_runs("AAA") == [
        {"name": "full", "first_trade_date": _day(0).isoformat(), "last_trade_date": _day(3).isoformat(), "trades": 2},
        {"name": "early", "first_trade_date": _day(0).isoformat(), "last_trade_date": _day(1).isoformat(), "trades": 1},
    ]
    assert buy_sell_pattern_runs("ZZZ") == []


def test_report_endpoint_pairs_trades_and_drops_ones_cut_by_range():
    from app.main import buy_sell_pattern_report

    _seed_bars("AAA", [(10, 10), (15, 20), (5, 5), (8, 12), (9, 11)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(4), "p1")
    # Stored trades: day0->day1, day2->day3, day4 same-day. Day 1 start cuts the first.
    result = buy_sell_pattern_report("AAA", "p1", _day(1).isoformat(), _day(4).isoformat())
    assert [bar["date"] for bar in result["bars"]] == [_day(i).isoformat() for i in range(1, 5)]
    assert (result["bars"][0]["open_datetime"], result["bars"][0]["close_datetime"]) == (
        _open(1).isoformat(),
        _close(1).isoformat(),
    )
    assert result["trades"] == [
        {
            "buy_datetime": _close(2).isoformat(),
            "buy_price": 5,
            "sell_datetime": _close(3).isoformat(),
            "sell_price": 12,
            "profit": 7,
            "profit_pct": 140,
        },
        {
            "buy_datetime": _open(4).isoformat(),
            "buy_price": 9,
            "sell_datetime": _close(4).isoformat(),
            "sell_price": 11,
            "profit": 2,
            "profit_pct": pytest.approx(200 / 9),
        },
    ]


def test_trades_endpoint_lists_every_trade_with_profit_pct():
    from app.main import buy_sell_pattern_trades

    _seed_bars("AAA", [(10, 10), (15, 20), (5, 5), (8, 12)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(3), "p1")
        # A $0 buy (bad bar) is still listed, with no percentage.
        pattern_id = session.query(BuySellPatternName.id).filter(BuySellPatternName.name == "p1").scalar()
        session.add_all(
            BuySellPattern(pattern_id=pattern_id, ticker="AAA", trade_datetime=at, buy_sell=side, price=price)
            for at, side, price in ((_open(10), "buy", 0.0), (_close(10), "sell", 3.0))
        )
        session.commit()
    trades = buy_sell_pattern_trades("AAA", "p1")
    assert [(t["buy_price"], t["sell_price"], t["profit"], t["profit_pct"]) for t in trades] == [
        (10, 20, 10, 100),
        (5, 12, 7, 140),
        (0, 3, 3, None),
    ]
    assert trades[0]["buy_datetime"] == _close(0).isoformat()
    assert buy_sell_pattern_trades("AAA", "missing") == []

    # Only trades entirely within the range; either bound may be left blank.
    def buys(**kwargs):
        return [t["buy_price"] for t in buy_sell_pattern_trades("AAA", "p1", **kwargs)]

    assert buys(start_date=_day(1).isoformat()) == [5, 0]
    assert buys(end_date=_day(2).isoformat()) == [10]
    assert buys(start_date=_day(0).isoformat(), end_date=_day(3).isoformat()) == [10, 5]
    with pytest.raises(HTTPException):
        buy_sell_pattern_trades("AAA", "p1", start_date=_day(3).isoformat(), end_date=_day(0).isoformat())


def test_report_endpoint_rejects_inverted_range():
    from app.main import buy_sell_pattern_report

    with pytest.raises(HTTPException) as exc_info:
        buy_sell_pattern_report("AAA", "p1", _day(3).isoformat(), _day(1).isoformat())
    assert exc_info.value.status_code == 422


def test_names_endpoint_lists_distinct_names_sorted():
    from app.main import buy_sell_pattern_names

    assert buy_sell_pattern_names() == []
    _seed_bars("AAA", [(10, 10), (15, 20)])
    _seed_bars("BBB", [(10, 10), (15, 20)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(1), "zeta")
        compute_buy_sell_pattern(session, _day(0), _day(1), "alpha")
    # Lists names from buy_sell_pattern_stats, so nothing until the stats job runs.
    assert buy_sell_pattern_names() == []
    with SessionLocal() as session:
        compute_buy_sell_pattern_stats(session)
    assert buy_sell_pattern_names() == ["alpha", "zeta"]


def test_tickers_endpoint_lists_each_tickers_stats_in_the_pattern():
    from app.main import buy_sell_pattern_tickers

    _seed_bars("AAA", [(10, 10), (15, 20), (5, 5), (8, 12)])
    _seed_bars("BBB", [(10, 10), (11, 12)], ticker_type="ETF")
    _seed_bars("CCC", [(10, 10), (15, 20)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(3), "p1", tickers=["AAA", "BBB"])
        compute_buy_sell_pattern(session, _day(0), _day(3), "p2", tickers=["CCC"])
        compute_buy_sell_pattern_stats(session)

    result = buy_sell_pattern_tickers("p1")
    assert result["total"] == 2
    # AAA: day0 close 10 -> day1 close 20, day2 close 5 -> day3 close 12.
    # BBB: day0 close 10 -> day1 close 12.
    aaa, bbb = result["rows"]
    computed_at = aaa.pop("computed_at")
    assert computed_at
    assert aaa == {
        "ticker": "AAA",
        "name": None,
        "type": "CS",
        "primary_exchange": None,
        # The filler bars after the seeded prices close at 1.
        "latest_close": 1,
        "trades": 2,
        # Per-trade profit %: 100, 140.
        "total_profit": pytest.approx(240),
        "first_trade_datetime": _close(0).isoformat(),
        "last_trade_datetime": _close(3).isoformat(),
        "avg_profit": pytest.approx(120),
        "avg_buy_price": 7.5,
        "avg_sell_price": 16,
        "buy_price_min": 5,
        "buy_price_max": 10,
        "buy_price_median": 7.5,
        "sell_price_min": 12,
        "sell_price_max": 20,
        "sell_price_median": 16,
    }
    assert (bbb["ticker"], bbb["type"], bbb["trades"], bbb["total_profit"]) == ("BBB", "ETF", 1, pytest.approx(20))
    assert (bbb["first_trade_datetime"], bbb["last_trade_datetime"]) == (_close(0).isoformat(), _close(1).isoformat())

    assert bbb["latest_close"] == 1
    assert [r["ticker"] for r in buy_sell_pattern_tickers("p1", ticker_types="ETF")["rows"]] == ["BBB"]
    assert [r["ticker"] for r in buy_sell_pattern_tickers("p1", tickers="AAA,CCC")["rows"]] == ["AAA"]
    assert [r["ticker"] for r in buy_sell_pattern_tickers("p1", order_by="avg_profit:desc")["rows"]] == ["AAA", "BBB"]
    assert [r["ticker"] for r in buy_sell_pattern_tickers("p1", order_by="type:desc")["rows"]] == ["BBB", "AAA"]
    assert [r["ticker"] for r in buy_sell_pattern_tickers("p1", order_by="ticker:desc")["rows"]] == ["BBB", "AAA"]
    with pytest.raises(HTTPException):
        buy_sell_pattern_tickers("p1", order_by="bogus")

    # A stats row whose ticker has no tickers row still shows, with null ticker details.
    with SessionLocal() as session:
        session.query(Ticker).filter(Ticker.ticker == "BBB").delete()
        session.commit()
    rows = buy_sell_pattern_tickers("p1")["rows"]
    assert [(r["ticker"], r["type"], r["trades"]) for r in rows] == [("AAA", "CS", 2), ("BBB", None, 1)]
    second_page = buy_sell_pattern_tickers("p1", page=2, page_size=1)
    assert (second_page["total"], [r["ticker"] for r in second_page["rows"]]) == (2, ["BBB"])
    assert buy_sell_pattern_tickers("missing") == {"rows": [], "total": 0, "page": 1, "page_size": 500}


def test_init_db_migrates_trade_date_to_trade_datetime():
    """conftest.py points the engine at a throwaway database, so recreating the table
    in its old shape here never touches the live one."""
    from sqlalchemy import inspect, text

    from db.session import engine

    _seed_bars("AAA", [(10, 10), (15, 20), (5, 9), (8, 12)])
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE buy_sell_patterns"))
        conn.execute(
            text(
                """
                CREATE TABLE buy_sell_patterns (
                    name VARCHAR NOT NULL, ticker VARCHAR NOT NULL, trade_date DATE NOT NULL,
                    buy_sell VARCHAR NOT NULL, price FLOAT, created_at DATETIME, updated_at DATETIME,
                    PRIMARY KEY (name, ticker, trade_date, buy_sell)
                )
                """
            )
        )
        rows = [
            # Equal open and close - the close wins.
            ("buy", _day(0), 10),
            # Matches day 1's close.
            ("sell", _day(1), 20),
            # Matches day 2's open; day 2's sell matches its close (a same-day trade).
            ("buy", _day(2), 5),
            ("sell", _day(2), 9),
            # No matching bar price - falls back to open for a buy, close for a sell.
            ("buy", _day(3), 7),
            ("sell", _day(3), 99),
        ]
        for buy_sell, day, price in rows:
            conn.execute(
                text(
                    "INSERT INTO buy_sell_patterns VALUES ('old', 'AAA', :d, :bs, :p, "
                    "'2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000')"
                ),
                {"d": day.isoformat(), "bs": buy_sell, "p": price},
            )

    init_db()

    # Both migrations ran: trade_date became trade_datetime, then name/created_at/
    # updated_at moved to buy_sell_pattern_names.
    columns = {col["name"] for col in inspect(engine).get_columns("buy_sell_patterns")}
    assert columns == {"pattern_id", "ticker", "trade_datetime", "buy_sell", "price"}
    with SessionLocal() as session:
        pattern = session.query(BuySellPatternName).one()
    assert (pattern.name, pattern.first_trade_datetime, pattern.last_trade_datetime) == ("old", _close(0), _close(3))
    assert pattern.created_at == pattern.updated_at == dt.datetime(2026, 1, 1)
    assert _rows("old") == [
        ("AAA", _close(0), "buy", 10),
        ("AAA", _close(1), "sell", 20),
        ("AAA", _open(2), "buy", 5),
        ("AAA", _close(2), "sell", 9),
        ("AAA", _open(3), "buy", 7),
        ("AAA", _close(3), "sell", 99),
    ]


def test_range_endpoints_compute_stats_on_the_fly_within_the_range():
    from app.main import buy_sell_pattern_pattern_names, buy_sell_pattern_range_tickers

    _seed_bars("AAA", [(10, 10), (15, 20), (5, 5), (8, 12)])
    _seed_bars("BBB", [(10, 10), (11, 12)], ticker_type="ETF")
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(3), "p1", tickers=["AAA", "BBB"])
    # Reads buy_sell_patterns directly - no buy-sell-pattern-stats run needed.
    assert buy_sell_pattern_pattern_names() == ["p1"]

    # Full range matches the stats job: AAA 10 -> 20, 5 -> 12; BBB 10 -> 12.
    full = buy_sell_pattern_range_tickers("p1", _day(0).isoformat(), _day(3).isoformat())
    assert full["total"] == 2
    aaa, bbb = full["rows"]
    assert aaa.pop("computed_at")
    assert aaa == {
        "ticker": "AAA",
        "name": None,
        "type": "CS",
        "primary_exchange": None,
        "latest_close": 1,
        "trades": 2,
        "total_profit": pytest.approx(240),
        "first_trade_datetime": _close(0).isoformat(),
        "last_trade_datetime": _close(3).isoformat(),
        "avg_profit": pytest.approx(120),
        "avg_buy_price": 7.5,
        "avg_sell_price": 16,
        "buy_price_min": 5,
        "buy_price_max": 10,
        "buy_price_median": 7.5,
        "sell_price_min": 12,
        "sell_price_max": 20,
        "sell_price_median": 16,
    }
    assert (bbb["ticker"], bbb["trades"], bbb["avg_profit"]) == ("BBB", 1, pytest.approx(20))

    # Only trades entirely inside the range count - day 2..3 keeps AAA's second trade.
    narrowed = buy_sell_pattern_range_tickers("p1", _day(2).isoformat(), _day(3).isoformat())
    assert [(r["ticker"], r["trades"], r["total_profit"]) for r in narrowed["rows"]] == [
        ("AAA", 1, pytest.approx(140))
    ]
    # Day 1..2 cuts both of AAA's trades and BBB's in half - nothing left.
    assert buy_sell_pattern_range_tickers("p1", _day(1).isoformat(), _day(2).isoformat())["total"] == 0

    assert [
        r["ticker"]
        for r in buy_sell_pattern_range_tickers(
            "p1", _day(0).isoformat(), _day(3).isoformat(), order_by="avg_profit:asc"
        )["rows"]
    ] == ["BBB", "AAA"]
    assert [
        r["ticker"]
        for r in buy_sell_pattern_range_tickers("p1", _day(0).isoformat(), _day(3).isoformat(), ticker_types="ETF")[
            "rows"
        ]
    ] == ["BBB"]
    assert [
        r["ticker"]
        for r in buy_sell_pattern_range_tickers("p1", _day(0).isoformat(), _day(3).isoformat(), tickers="AAA")["rows"]
    ] == ["AAA"]
    with pytest.raises(HTTPException):
        buy_sell_pattern_range_tickers("p1", _day(3).isoformat(), _day(0).isoformat())


def test_compute_records_pattern_name_row_and_keeps_it_on_replace():
    from app.main import buy_sell_pattern_pattern_names

    _seed_bars("AAA", [(10, 10), (15, 20), (5, 5), (8, 12)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(3), "p1")
    with SessionLocal() as session:
        first = session.query(BuySellPatternName).one()
    assert (first.name, first.first_trade_datetime, first.last_trade_datetime) == ("p1", _close(0), _close(3))
    assert first.created_at == first.updated_at
    assert buy_sell_pattern_pattern_names() == ["p1"]

    # Replace keeps the id and created_at, moves updated_at and the span.
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(1), "p1", replace=True)
    with SessionLocal() as session:
        replaced = session.query(BuySellPatternName).one()
    assert (replaced.id, replaced.created_at) == (first.id, first.created_at)
    assert replaced.updated_at >= first.updated_at
    assert (replaced.first_trade_datetime, replaced.last_trade_datetime) == (_close(0), _close(1))
    assert {pattern_id for (pattern_id,) in SessionLocal().query(BuySellPattern.pattern_id)} == {first.id}

    # A replacing run with no trades removes the name, as it used to remove its rows.
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(0), "p1", tickers=["AAA"], replace=True)
    assert _rows("p1") == []
    assert buy_sell_pattern_pattern_names() == []
    with SessionLocal() as session:
        assert not pattern_name_exists(session, "p1")
