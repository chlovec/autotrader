SELECT
    a.ticker,
    a.name,
    a.type,
    a.market,
    a.active,
    b.timestamp,
    CAST(strftime('%Y', b.timestamp) AS INTEGER) AS year,
    CAST(strftime('%m', b.timestamp) AS INTEGER) AS month,
    CAST(strftime('%d', b.timestamp) AS INTEGER) AS day,
    b.open AS Start_Price,
    b.close AS close_Price,
    lp.latest_price,
    (b.close - b.open) / NULLIF(b.open, 0) * 100 AS pcnt_diff,
    CASE
        WHEN (b.close - b.open) / NULLIF(b.open, 0) * 100 < -2   THEN 'strong down'
        WHEN (b.close - b.open) / NULLIF(b.open, 0) * 100 < -0.5 THEN 'down'
        WHEN (b.close - b.open) / NULLIF(b.open, 0) * 100 <= 0.5 THEN 'neutral'
        WHEN (b.close - b.open) / NULLIF(b.open, 0) * 100 <= 2   THEN 'up'
        ELSE 'strong up'
    END AS market_type
FROM tickers as a
join ohlc_bars as b on a.ticker = b.ticker
LEFT JOIN (
    SELECT o.ticker, o.close AS latest_price
    FROM ohlc_bars o
    JOIN (
        SELECT ticker, MAX(timestamp) AS timestamp
        FROM ohlc_bars
        GROUP BY ticker
    ) latest ON o.ticker = latest.ticker AND o.timestamp = latest.timestamp
) lp ON lp.ticker = a.ticker
WHERE a.type in ('CS', 'PFD', 'BOND', 'OS', 'AGEN')
ORDER BY a.ticker, b.timestamp;