WITH
-- Only daily bars matter; every step below reads from this.
daily_bars AS (
    SELECT *
    FROM ohlc_bars
    WHERE multiplier = 1 AND timespan = 'day'
),

-- The most recent trading day in the data.
latest_day AS (
    SELECT MAX(timestamp) AS ts
    FROM daily_bars
),

-- Filter 1: tickers that have a bar on the latest day.
current_tickers AS (
    SELECT ticker
    FROM daily_bars
    WHERE timestamp = (SELECT ts FROM latest_day)
),

-- Filter 2: of those, tickers whose 60 most recent bars all exist.
eligible_tickers AS (
    SELECT c.ticker
    FROM current_tickers c
    WHERE (
        SELECT COUNT(*)
        FROM (
            SELECT 1
            FROM daily_bars d
            WHERE d.ticker = c.ticker
            ORDER BY d.timestamp DESC
            LIMIT 60
        ) AS recent
    ) = 60
)

SELECT
    b.ticker,
    date(b.timestamp)                            AS date,
    CAST(strftime('%Y', b.timestamp) AS INTEGER) AS year,
    CAST(strftime('%m', b.timestamp) AS INTEGER) AS month,
    CAST(strftime('%d', b.timestamp) AS INTEGER) AS day,
    b.open,
    b.close,
    b.pcnt_increase,
    CASE
        WHEN b.pcnt_increase <  -20  THEN -3  -- Very Strong Down
        WHEN b.pcnt_increase <  -5   THEN -2  -- Strong Down
        WHEN b.pcnt_increase <  -0.1 THEN -1  -- Down
        WHEN b.pcnt_increase <=  0.1 THEN  0  -- Neutral
        WHEN b.pcnt_increase <=  5   THEN  1  -- Up
        WHEN b.pcnt_increase <=  20  THEN  2  -- Strong Up
        WHEN b.pcnt_increase >   20  THEN  3  -- Very Strong Up
    END AS direction
FROM daily_bars b
WHERE b.ticker IN (SELECT ticker FROM eligible_tickers)
ORDER BY b.ticker, b.timestamp;
