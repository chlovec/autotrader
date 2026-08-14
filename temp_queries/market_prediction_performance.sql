SELECT
	a.ticker,
	a.name,
	a.market,
	a.locale,
	a.type,
	b.description,
	a.active,
	a.currency_name,
	a.primary_exchange,
	g.market_cap,
	h.average_volume,
	c.predicted_date,
	c.current_state as markov_current_state,
	c.predicted_state as markov_predicted_state,
	c.state_confidence as markov_state_confidence,
	c.expected_return as markov_expected_return,
	c.entry_price as markov_entry_price,
	c.exit_price as markov_exit_price,
	c.history_days as markov_history_days,
	c.exit_price_confidence as markov_exit_price_confidence,
	d.current_state as mcmc_current_state,
	d.state_confidence as mcmc_state_confidence,
	d.expected_return as mcmc_expected_return,
	d.entry_price as mcmc_entry_price,
	d.exit_price as mcmc_exit_price,
	d.history_days as mcmc_history_days,
	d.exit_price_confidence as mcmc_exit_price_confidence,
	e.open as actual_entry_price,
	e.close as actual_exit_price,
	e.pcnt_increase as actual_gain,
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
	END as mcmc_result,
	f.mcmc_win_count,
	f.mcmc_win_rate,
	f.mcmc_predictions_count,
	f.markov_win_count,
	f.markov_win_rate,
	f.markov_predictions_count
FROM tickers a
JOIN ticker_types b
	on a.type = b.code
JOIN market_predictions c
	on a.ticker = c.ticker
LEFT JOIN market_predictions_mcmc d
	on c.ticker = d.ticker and c.predicted_date = d.predicted_date
LEFT OUTER JOIN ohlc_bars e
	on c.ticker = e.ticker and c.ticker = e.ticker and c.predicted_date = date(e.timestamp)
LEFT OUTER JOIN win_rates f
	on a.ticker = f.ticker
LEFT OUTER JOIN ticker_details g
	on a.ticker = g.ticker
LEFT OUTER JOIN (
	SELECT av.ticker, av.average_volume
	FROM average_volumes av
	JOIN (
		SELECT ticker, MAX(computed_at) as computed_at
		FROM average_volumes
		GROUP BY ticker
	) latest
		on av.ticker = latest.ticker and av.computed_at = latest.computed_at
) h
	on a.ticker = h.ticker
WHERE c.predicted_date BETWEEN :start_date AND :end_date
	AND (:types IS NULL OR a.type IN (SELECT value FROM json_each(:types)))
	AND (:tickers IS NULL OR a.ticker IN (SELECT value FROM json_each(:tickers)));
