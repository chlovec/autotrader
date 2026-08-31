SELECT
    ticker,
    date(timestamp) AS date,
    Start_Price AS open_price,
    close_Price AS close_price,
    pcnt_diff,
    market_type
FROM tickers_daily_market_direction
WHERE ticker = :ticker
    AND date(timestamp) BETWEEN :start_date AND :end_date
ORDER BY timestamp;
