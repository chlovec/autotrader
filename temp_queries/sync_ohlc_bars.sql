SELECT
    a.ticker,
    -- Calculate new start date
    CASE 
        WHEN a.last_ohlc_sync_date IS NULL THEN p.start_date
        WHEN date(a.last_ohlc_sync_date, '+1 day') > p.start_date THEN date(a.last_ohlc_sync_date, '+1 day')
        ELSE p.start_date
    END AS new_start_date,
    -- Set end date
    p.end_date AS new_end_date
FROM tickers AS a
JOIN ticker_types AS b
    ON a.type = b.code
CROSS JOIN params AS p
WHERE 
    a.last_ohlc_sync_date IS NULL
    OR a.last_ohlc_sync_date < p.end_date
ORDER BY b.rank, a.ticker
LIMIT 1000