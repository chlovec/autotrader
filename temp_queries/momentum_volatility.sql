-- Per-ticker daily momentum and volatility over a user-supplied lookback.
-- Parameter:
--   :window  number of trading days (bars) to look back, e.g. 20
-- Both are computed only from previous_close (never the current bar's close),
-- so they're known at the open and safe to use as features for that day.
-- momentum   = previous_close / previous_close :window bars ago - 1
-- volatility = sample std dev of the last :window returns of previous_close
-- Both are NULL until a ticker has enough history to fill the window.
WITH
-- Only daily bars matter; every step below reads from this.
daily_bars AS (
    SELECT ticker, timestamp, close
    FROM ohlc_bars
    WHERE multiplier = 1 AND timespan = 'day'
),

-- Previous close for each bar; everything after this uses only previous_close.
with_prev_close AS (
    SELECT
        ticker,
        timestamp,
        LAG(close, 1) OVER (PARTITION BY ticker ORDER BY timestamp) AS previous_close
    FROM daily_bars
),

-- previous_close :window bars ago, and the return of previous_close vs. the bar before.
with_lags AS (
    SELECT
        ticker,
        timestamp,
        previous_close,
        LAG(previous_close, :window) OVER w AS window_start_close,
        1.0 * previous_close / NULLIF(LAG(previous_close, 1) OVER w, 0) - 1 AS prev_return
    FROM with_prev_close
    WINDOW w AS (PARTITION BY ticker ORDER BY timestamp)
),

-- Rolling sums over the last :window previous_close returns (SQLite has no STDEV).
rolling AS (
    SELECT
        *,
        COUNT(prev_return)             OVER r AS n,
        SUM(prev_return)               OVER r AS s,
        SUM(prev_return * prev_return) OVER r AS ss
    FROM with_lags
    WINDOW r AS (PARTITION BY ticker ORDER BY timestamp
                 ROWS BETWEEN :window - 1 PRECEDING AND CURRENT ROW)
)

SELECT
    ticker,
    timestamp,
    previous_close,
    1.0 * previous_close / NULLIF(window_start_close, 0) - 1 AS momentum,
    CASE
        WHEN n = :window AND n > 1
        THEN sqrt(MAX((ss - s * s / n) / (n - 1), 0))
    END                                                       AS volatility
FROM rolling
ORDER BY ticker, timestamp;
