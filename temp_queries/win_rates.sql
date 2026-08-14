SELECT 
	a.ticker
	CASE
		WHEN e.pcnt_increase IS NULL THEN NULL
		WHEN e.pcnt_increase <= 0  AND c.expected_return <= 0 THEN 'WON'
		WHEN e.pcnt_increase >= 0  AND c.expected_return >= 0 THEN 'WIN'
		ELSE 'FAILED'
	END as markov_result,
	CASE
		WHEN e.pcnt_increase IS NULL THEN NULL
		WHEN e.pcnt_increase <= 0  AND d.expected_return <= 0 THEN 'WON'
		WHEN e.pcnt_increase >= 0  AND d.expected_return >= 0 THEN 'WIN'
		ELSE 'FAILED'
	END as mcmc_result
FROM tickers a
JOIN market_predictions c
	on a.ticker = c.ticker
LEFT JOIN market_predictions_mcmc d
	on c.ticker = d.ticker and c.predicted_date = d.predicted_date
LEFT OUTER JOIN ohlc_bars e
	on c.ticker = e.ticker and c.ticker = e.ticker and c.predicted_date = date(e.timestamp);
