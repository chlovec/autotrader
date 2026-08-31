SELECT
    t.ticker,
    t.name,
    t.type,
    t.market,
    MAX(t.latest_price) AS latest_price,
    MAX(d.market_cap) AS market_cap,
    COUNT(*) AS total_records,
    100.0 * SUM(CASE WHEN t.market_type = 'strong down' THEN 1 ELSE 0 END) / COUNT(*) AS pcnt_strong_down,
    100.0 * SUM(CASE WHEN t.market_type = 'down' THEN 1 ELSE 0 END) / COUNT(*) AS pcnt_down,
    100.0 * SUM(CASE WHEN t.market_type = 'neutral' THEN 1 ELSE 0 END) / COUNT(*) AS pcnt_neutral,
    100.0 * SUM(CASE WHEN t.market_type = 'up' THEN 1 ELSE 0 END) / COUNT(*) AS pcnt_up,
    100.0 * SUM(CASE WHEN t.market_type = 'strong up' THEN 1 ELSE 0 END) / COUNT(*) AS pcnt_strong_up
FROM tickers_daily_market_direction t
LEFT JOIN ticker_details d ON d.ticker = t.ticker
WHERE date(t.timestamp) BETWEEN :start_date AND :end_date
    AND (:types IS NULL OR t.type IN (SELECT value FROM json_each(:types)))
    AND (:tickers IS NULL OR t.ticker IN (SELECT value FROM json_each(:tickers)))
GROUP BY t.ticker, t.name, t.type, t.market
ORDER BY t.ticker;
