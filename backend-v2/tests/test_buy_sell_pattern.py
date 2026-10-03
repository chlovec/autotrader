import datetime as dt
import random

import pytest
from fastapi import HTTPException

from db.models import BuySellPattern, JobConfig, OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.buy_sell_pattern import (
    DailyBar,
    PatternNameConflict,
    Trade,
    compute_buy_sell_pattern,
    default_pattern_name,
    max_profit_trades,
    resolve_pattern_name,
    validate_date_range,
)
from jobs.registry import BUY_SELL_PATTERN_JOB


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(BuySellPattern).delete()
    session.query(OhlcBar).delete()
    session.query(JobConfig).delete()
    session.query(Ticker).delete()
    session.commit()
    session.close()
    yield


_START = dt.date(2026, 1, 5)


def _day(i: int) -> dt.date:
    return _START + dt.timedelta(days=i)


def _bars(*lhc: tuple[float, float, float]) -> list[DailyBar]:
    return [DailyBar(_day(i), low, high, close) for i, (low, high, close) in enumerate(lhc)]


def _total(trades: list[Trade]) -> float:
    return sum(t.profit for t in trades)


def _brute_force(bars: list[DailyBar]) -> float:
    """Exhaustive search over the same rules as max_profit_trades: from day i with
    nothing held, either skip day i, or buy at low[i] and sell at high[j] for some j > i
    (or j == i when close[i] > low[i]), then continue from day j + 1."""
    n = len(bars)
    best = [0.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        best[i] = best[i + 1]
        for j in range(i, n):
            if j == i and not bars[i].close > bars[i].low:
                continue
            best[i] = max(best[i], bars[j].high - bars[i].low + best[j + 1])
    return best[0]


def test_empty_and_single_flat_day_have_no_trades():
    assert max_profit_trades([]) == []
    # close == low - no same-day trade allowed, and no later day to sell on.
    assert max_profit_trades(_bars((10, 12, 10))) == []


def test_same_day_trade_when_close_above_low():
    trades = max_profit_trades(_bars((10, 12, 11)))
    assert trades == [Trade(_day(0), 10, _day(0), 12)]


def test_no_same_day_trade_when_close_not_above_low_sells_later_instead():
    # Day 0 closes at its low, so it can't sell the same day - buys day 0, sells day 1.
    trades = max_profit_trades(_bars((10, 12, 10), (11, 13, 11)))
    assert trades == [Trade(_day(0), 10, _day(1), 13)]


def test_holds_across_days_when_that_beats_separate_trades():
    # Separate same-day trades: (11-10) + (21-20) = 2. Holding: 21 - 10 = 11.
    trades = max_profit_trades(_bars((10, 11, 10.5), (20, 21, 20.5)))
    assert trades == [Trade(_day(0), 10, _day(1), 21)]


def test_multiple_non_overlapping_trades():
    bars = _bars((10, 10, 10), (15, 20, 15), (5, 5, 5), (8, 12, 8))
    trades = max_profit_trades(bars)
    assert trades == [Trade(_day(0), 10, _day(1), 20), Trade(_day(2), 5, _day(3), 12)]
    for earlier, later in zip(trades, trades[1:]):
        assert later.buy_date > earlier.sell_date


def test_falling_prices_with_closes_at_lows_have_no_trades():
    assert max_profit_trades(_bars((10, 10, 10), (9, 9, 9), (8, 8, 8))) == []


def test_matches_brute_force_on_random_series():
    rng = random.Random(42)
    for _ in range(300):
        bars = []
        price = 50.0
        for i in range(rng.randint(1, 12)):
            price = max(1.0, price + rng.uniform(-5, 5))
            low = price - rng.uniform(0, 3)
            high = price + rng.uniform(0, 3)
            close = rng.choice([low, rng.uniform(low, high)])
            bars.append(DailyBar(_day(i), low, high, close))
        trades = max_profit_trades(bars)
        assert _total(trades) == pytest.approx(_brute_force(bars))
        for trade in trades:
            assert trade.profit > 0
            assert trade.sell_date >= trade.buy_date
        for earlier, later in zip(trades, trades[1:]):
            assert later.buy_date > earlier.sell_date


def test_resolve_pattern_name():
    start, end = dt.date(2026, 1, 1), dt.date(2026, 3, 31)
    assert default_pattern_name(start, end) == "2026-01-01_2026-03-31"
    assert resolve_pattern_name("mine", start, end, "manual") == "mine"
    assert resolve_pattern_name("  ", start, end, "manual") == "2026-01-01_2026-03-31"
    assert resolve_pattern_name(None, start, end, "manual") == "2026-01-01_2026-03-31"
    # Auto runs never use the override.
    assert resolve_pattern_name("mine", start, end, "auto") == "2026-01-01_2026-03-31"


def test_validate_date_range():
    with pytest.raises(ValueError):
        validate_date_range(None, dt.date(2026, 1, 1))
    with pytest.raises(ValueError):
        validate_date_range(dt.date(2026, 1, 2), dt.date(2026, 1, 1))
    assert validate_date_range(dt.date(2026, 1, 1), dt.date(2026, 1, 1)) == (dt.date(2026, 1, 1), dt.date(2026, 1, 1))


def _seed_bars(ticker: str, lhc: list[tuple[float, float, float]], ticker_type: str = "CS") -> None:
    with SessionLocal() as session:
        session.add(Ticker(ticker=ticker, type=ticker_type))
        for i, (low, high, close) in enumerate(lhc):
            session.add(
                OhlcBar(
                    ticker=ticker,
                    multiplier=1,
                    timespan="day",
                    timestamp=dt.datetime.combine(_day(i), dt.time.min),
                    open=low,
                    low=low,
                    high=high,
                    close=close,
                )
            )
        session.commit()


def _rows(name: str) -> list[tuple[str, dt.date, str, float]]:
    with SessionLocal() as session:
        return [
            (r.ticker, r.trade_date, r.buy_sell, r.price)
            for r in session.query(BuySellPattern)
            .filter(BuySellPattern.name == name)
            .order_by(BuySellPattern.ticker, BuySellPattern.trade_date, BuySellPattern.buy_sell)
        ]


def test_compute_stores_buy_and_sell_rows_within_range():
    _seed_bars("AAA", [(10, 10, 10), (15, 20, 15), (5, 5, 5), (8, 12, 8), (1, 100, 1)])
    with SessionLocal() as session:
        # End date excludes day 4's huge high.
        result = compute_buy_sell_pattern(session, _day(0), _day(3), "p1")
    assert (result.tickers, result.trades, result.replaced) == (1, 2, False)
    assert _rows("p1") == [
        ("AAA", _day(0), "buy", 10),
        ("AAA", _day(1), "sell", 20),
        ("AAA", _day(2), "buy", 5),
        ("AAA", _day(3), "sell", 12),
    ]


def test_compute_same_day_trade_stores_buy_and_sell_on_same_date():
    _seed_bars("AAA", [(10, 12, 11)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(0), "p1")
    assert _rows("p1") == [("AAA", _day(0), "buy", 10), ("AAA", _day(0), "sell", 12)]


def test_compute_respects_ticker_selection():
    _seed_bars("AAA", [(10, 12, 11)])
    _seed_bars("BBB", [(10, 12, 11)], ticker_type="ETF")
    with SessionLocal() as session:
        result = compute_buy_sell_pattern(session, _day(0), _day(0), "p1", ticker_types=["ETF"])
    assert result.tickers == 1
    assert {row[0] for row in _rows("p1")} == {"BBB"}


def test_compute_name_conflict_fails_without_replace_and_keeps_rows():
    _seed_bars("AAA", [(10, 12, 11)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(0), "p1")
    before = _rows("p1")
    with SessionLocal() as session, pytest.raises(PatternNameConflict):
        compute_buy_sell_pattern(session, _day(0), _day(0), "p1")
    assert _rows("p1") == before


def test_compute_replace_rewrites_all_rows_for_name():
    _seed_bars("AAA", [(10, 12, 11)])
    _seed_bars("BBB", [(10, 12, 11)])
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
    _seed_bars("AAA", [(10, 12, 11)])
    _seed_bars("BBB", [(10, 12, 11)])
    with SessionLocal() as session, pytest.raises(RuntimeError):
        compute_buy_sell_pattern(session, _day(0), _day(0), "p1", batch_size=1, control=_FailOnBatch(2))
    assert _rows("p1") == []
    # The staging table is cleaned up, so the same name can run again straight away.
    with SessionLocal() as session:
        result = compute_buy_sell_pattern(session, _day(0), _day(0), "p1", batch_size=1)
    assert result.trades == 2
    assert {row[0] for row in _rows("p1")} == {"AAA", "BBB"}


def test_compute_replace_error_keeps_old_rows():
    _seed_bars("AAA", [(10, 12, 11)])
    _seed_bars("BBB", [(10, 12, 11)])
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

    _seed_bars("AAA", [(10, 12, 11)])
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

    _seed_bars("AAA", [(10, 12, 11)])
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


def test_trigger_job_requires_date_range():
    from app.main import JobRunOverridesIn, trigger_job

    with pytest.raises(HTTPException) as exc_info:
        trigger_job(BUY_SELL_PATTERN_JOB, JobRunOverridesIn())
    assert exc_info.value.status_code == 400


def test_runs_endpoint_lists_names_with_spans_newest_first():
    from app.main import buy_sell_pattern_runs

    _seed_bars("AAA", [(10, 10, 10), (15, 20, 15), (5, 5, 5), (8, 12, 8)])
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

    _seed_bars("AAA", [(10, 10, 10), (15, 20, 15), (5, 5, 5), (8, 12, 8), (9, 11, 10)])
    with SessionLocal() as session:
        compute_buy_sell_pattern(session, _day(0), _day(4), "p1")
    # Stored trades: day0->day1, day2->day3, day4 same-day. Day 1 start cuts the first.
    result = buy_sell_pattern_report("AAA", "p1", _day(1).isoformat(), _day(4).isoformat())
    assert [bar["date"] for bar in result["bars"]] == [_day(i).isoformat() for i in range(1, 5)]
    assert result["trades"] == [
        {"buy_date": _day(2).isoformat(), "buy_price": 5, "sell_date": _day(3).isoformat(), "sell_price": 12, "profit": 7},
        {"buy_date": _day(4).isoformat(), "buy_price": 9, "sell_date": _day(4).isoformat(), "sell_price": 11, "profit": 2},
    ]


def test_report_endpoint_rejects_inverted_range():
    from app.main import buy_sell_pattern_report

    with pytest.raises(HTTPException) as exc_info:
        buy_sell_pattern_report("AAA", "p1", _day(3).isoformat(), _day(1).isoformat())
    assert exc_info.value.status_code == 422
