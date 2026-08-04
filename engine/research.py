"""Symbol screening: scores how worth-trading a symbol is, for engine/research_runner.py
to rank a universe and persist the results (db.models.ResearchResult).

Two independent scorers, not a plugin/ABC system like engine/strategy.py's Strategy -
there are only two of them and no evidence more are coming, so keep this as plain
functions rather than build an abstraction for a single implementation.
"""

from __future__ import annotations

import datetime as dt
import re

import pandas as pd
from alpaca.data.historical.news import NewsClient
from alpaca.data.models.news import News
from alpaca.data.requests import NewsRequest

from engine.config import Config

# --- technical screen -------------------------------------------------------------

# Rough bands used to normalize each raw metric into 0-100 before blending. Not fitted to
# any dataset - just wide enough that a genuinely exceptional or terrible reading saturates
# instead of the score being dominated by one outlier metric.
_RETURN_BAND = 0.5  # +/-50% over the lookback window maps to the 0-100 ends
_MOMENTUM_QUALITY_BAND = 3.0  # return / volatility ratio, +/-3 maps to the 0-100 ends
_MIN_DOLLAR_VOLUME = 1_000_000  # daily $ turnover below this scores near 0 (illiquid)
_MAX_DOLLAR_VOLUME = 200_000_000  # at/above this scores 100 (highly liquid)


def _clip_scale(value: float, low: float, high: float) -> float:
    """Linearly maps value from [low, high] to [0, 100], clipping outside that range."""
    if high == low:
        return 50.0
    fraction = (value - low) / (high - low)
    return max(0.0, min(1.0, fraction)) * 100


def score_technical(bars: pd.DataFrame, lookback: int = 60) -> tuple[float, str]:
    """Blends lookback return, return/volatility ratio (momentum quality - rewards steady
    trends over one wild day), and average dollar volume (liquidity floor) into a 0-100
    score. Requires 'close' and 'volume' columns, ascending by time."""
    if len(bars) < lookback + 1:
        return 0.0, f"not enough history for technical screen ({len(bars)}/{lookback + 1} bars)"

    window = bars.tail(lookback + 1)
    closes = window["close"]

    lookback_return = closes.iloc[-1] / closes.iloc[0] - 1
    daily_returns = closes.pct_change().dropna()
    volatility = daily_returns.std()
    momentum_quality = (lookback_return / volatility) if volatility > 1e-9 else 0.0
    avg_dollar_volume = (window["close"] * window["volume"]).tail(lookback).mean()

    return_score = _clip_scale(lookback_return, -_RETURN_BAND, _RETURN_BAND)
    momentum_score = _clip_scale(momentum_quality, -_MOMENTUM_QUALITY_BAND, _MOMENTUM_QUALITY_BAND)
    liquidity_score = _clip_scale(avg_dollar_volume, _MIN_DOLLAR_VOLUME, _MAX_DOLLAR_VOLUME)

    technical_score = 0.4 * return_score + 0.4 * momentum_score + 0.2 * liquidity_score
    reason = (
        f"{lookback}d return={lookback_return:.1%}, momentum_quality={momentum_quality:.2f}, "
        f"avg_dollar_volume=${avg_dollar_volume:,.0f}"
    )
    return technical_score, reason


# --- news/sentiment screen ---------------------------------------------------------

# Deterministic keyword sentiment, matching this codebase's existing style - strategy.py's
# RSI/ADX indicators are formulas too, not ML. Has real limits worth knowing before trusting
# it: no negation handling ("not profitable" reads as positive via "profitable"), no sarcasm,
# and it treats every source as equally credible. Good enough as one input among several in
# a blended score; not a substitute for actually reading the news.
_POSITIVE_WORDS = {
    "beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies", "growth",
    "profit", "profitable", "upgrade", "upgraded", "outperform", "record", "strong",
    "gain", "gains", "bullish", "expansion", "raises", "raised", "exceeds", "wins",
}
_NEGATIVE_WORDS = {
    "miss", "misses", "plunge", "plunges", "crash", "crashes", "downgrade", "downgraded",
    "underperform", "weak", "loss", "losses", "bearish", "lawsuit", "recall", "layoffs",
    "cuts", "cut", "investigation", "fraud", "bankruptcy", "decline", "declines", "fall", "falls",
}
_WORD_RE = re.compile(r"[a-z]+")

_COVERAGE_CAP = 10  # articles beyond this no longer add to the coverage/attention bonus


def _polarity(text: str) -> int:
    words = set(_WORD_RE.findall(text.lower()))
    return len(words & _POSITIVE_WORDS) - len(words & _NEGATIVE_WORDS)


def score_news(articles: list[News]) -> tuple[float, str]:
    """Keyword sentiment + coverage volume over recent headlines/summaries, blended into a
    0-100 score centered on 50 (neutral). An empty article list scores neutral (50), not 0 -
    otherwise thinly-covered but solid names (and ETFs) would be systematically punished
    relative to whatever's currently generating headline noise."""
    if not articles:
        return 50.0, "no news coverage in window, neutral score"

    polarities = [_polarity(f"{a.headline} {a.summary}") for a in articles]
    avg_polarity = sum(polarities) / len(polarities)
    # Average polarity per article rarely exceeds +/-3 in practice, so that's the band
    # mapped onto 0-100 (50 = neutral) without every article needing to be uniformly
    # one-sided to saturate the score.
    sentiment_score = _clip_scale(avg_polarity, -3, 3)
    coverage_bonus = min(len(articles), _COVERAGE_CAP) / _COVERAGE_CAP * 10  # up to +10 for being covered at all

    news_score = max(0.0, min(100.0, sentiment_score + coverage_bonus))
    lean = "positive" if avg_polarity > 0 else "negative" if avg_polarity < 0 else "neutral"
    reason = f"{len(articles)} articles, avg sentiment {lean} ({avg_polarity:+.1f}/article)"
    return news_score, reason


def make_news_client(config: Config) -> NewsClient:
    """Alpaca News API client. All alpaca-py news usage is confined to this file, the way
    broker SDK usage is confined to engine/brokers/*_broker.py.

    Uses config.alpaca_news_api_key/alpaca_news_secret_key - a global key pair independent
    of any account's own broker credentials (see engine/config.py), since research screens
    one shared universe regardless of which broker(s) any account actually trades through."""
    return NewsClient(config.alpaca_news_api_key, config.alpaca_news_secret_key)


def fetch_universe_news(client: NewsClient, symbols: list[str], lookback_days: int = 14) -> dict[str, list[News]]:
    """Fetches recent news for a batch of symbols in one call and buckets it per symbol.
    NewsClient.get_news() auto-paginates internally, and NewsRequest.symbols takes a
    comma-joined string - so this is one API round trip regardless of batch size, not one
    call per symbol. A single article commonly tags multiple tickers, so bucketing checks
    membership in article.symbols rather than assuming a 1:1 grouping.

    Called once per research_runner.research_once() batch (research_runner.BATCH_SIZE
    symbols at a time), not once for the whole universe - the stored universe is commonly
    thousands of symbols (see engine/universe_sync.py), and a single comma-joined request
    for all of them at once wouldn't be a reasonably-sized API call."""
    request = NewsRequest(
        symbols=",".join(symbols),
        start=dt.datetime.utcnow() - dt.timedelta(days=lookback_days),
        limit=200,
    )
    articles = client.get_news(request).data.get("news", [])

    by_symbol: dict[str, list[News]] = {symbol: [] for symbol in symbols}
    for article in articles:
        for symbol in article.symbols:
            if symbol in by_symbol:
                by_symbol[symbol].append(article)
    return by_symbol


def combine(technical_score: float, news_score: float, technical_weight: float = 0.5, news_weight: float = 0.5) -> float:
    """Named float params rather than a weights tuple, so a call site can't accidentally
    transpose (news_weight, technical_weight)."""
    return technical_weight * technical_score + news_weight * news_score
