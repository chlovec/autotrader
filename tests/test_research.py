import datetime as dt

import pandas as pd
from alpaca.data.models.news import News

from engine.research import combine, score_news, score_technical


def _bars(closes: list[float], volume: float = 1_000_000) -> pd.DataFrame:
    return pd.DataFrame(
        {"close": closes, "volume": [volume] * len(closes)},
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )


def _news(headline: str, summary: str = "") -> News:
    return News(
        raw_data={
            "id": 1,
            "headline": headline,
            "source": "test",
            "url": None,
            "summary": summary,
            "created_at": dt.datetime(2026, 1, 1),
            "updated_at": dt.datetime(2026, 1, 1),
            "symbols": ["AAPL"],
            "author": "",
            "content": "",
        }
    )


def test_technical_not_enough_history():
    score, reason = score_technical(_bars([100] * 10), lookback=60)
    assert score == 0.0
    assert "not enough history" in reason


def test_technical_rewards_uptrend_over_flat():
    uptrend = _bars([100 + i for i in range(61)])
    flat = _bars([100] * 61)
    uptrend_score, _ = score_technical(uptrend, lookback=60)
    flat_score, _ = score_technical(flat, lookback=60)
    assert uptrend_score > flat_score


def test_technical_rewards_liquidity():
    liquid, _ = score_technical(_bars([100] * 61, volume=10_000_000), lookback=60)
    illiquid, _ = score_technical(_bars([100] * 61, volume=1_000), lookback=60)
    assert liquid > illiquid


def test_news_empty_list_is_neutral():
    score, reason = score_news([])
    assert score == 50.0
    assert "no news coverage" in reason


def test_news_positive_headlines_score_above_neutral():
    articles = [_news("Company beats earnings, shares surge on strong growth")]
    score, reason = score_news(articles)
    assert score > 50.0
    assert "positive" in reason


def test_news_negative_headlines_score_below_neutral():
    articles = [_news("Company misses earnings, faces lawsuit amid fraud investigation")]
    score, reason = score_news(articles)
    assert score < 50.0
    assert "negative" in reason


def test_combine_weights():
    assert combine(100.0, 0.0, technical_weight=1.0, news_weight=0.0) == 100.0
    assert combine(0.0, 100.0, technical_weight=0.0, news_weight=1.0) == 100.0
    assert combine(80.0, 40.0) == 60.0
