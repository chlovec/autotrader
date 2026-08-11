import type { NumericFilterOp } from './numericFilter'

// backend-v2's API (app/main.py), launched via bin/restart-v2.sh's run_jobs.py -
// separate from v1's frontend/src/api.ts, which points at the repo-root backend instead.
export const API_BASE = import.meta.env.VITE_BACKEND_V2_URL ?? 'http://localhost:8001'

export type RunType = 'manual' | 'auto'
export type ScheduleIntervalUnit = 'minutes' | 'hours' | 'days'

// Quarter-hour UTC time-of-day slots, "00:00".."23:45" - mirrors backend-v2's
// jobs/registry.py START_TIME_OPTIONS, which app/main.py validates JobConfigIn.start_time
// against. Generated rather than fetched: it's static and fully determined by this one
// formula, so there's nothing a network round trip would keep in sync that this doesn't
// already guarantee by construction.
export const START_TIME_OPTIONS: string[] = Array.from({ length: 24 * 4 }, (_, i) => {
  const hour = Math.floor(i / 4)
  const minute = (i % 4) * 15
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
})

export interface JobRun {
  id: number
  trigger: RunType
  status: 'in_progress' | 'completed' | 'failed' | 'cancelled'
  started_at: string
  finished_at: string | null
  duration_seconds: number | null
  result_summary: string | null
  error: string | null
  // Only non-null for jobs that know their full unit count up front and report as
  // they go (see backend-v2 jobs/control.py's report_job_progress) - sync-bars-nightly,
  // sync-snapshots, sync-ticker-details, the technical-indicator jobs. null for every
  // other job, and briefly null even for those until the first progress write lands.
  progress_completed: number | null
  progress_total: number | null
}

export interface Job {
  name: string
  label: string
  description: string
  has_bars_fields: boolean
  has_ticker_type_filter: boolean
  has_ticker_selector: boolean
  has_snapshot_type_filter: boolean
  has_average_volume_fields: boolean
  has_backtest_fields: boolean
  has_prediction_start_date_field: boolean
  has_predicted_date_offset_field: boolean
  has_monte_carlo_fields: boolean
  // Always the full massive.com asset-class list (jobs/registry.py's
  // SNAPSHOT_TYPE_OPTIONS), regardless of has_snapshot_type_filter - fetched from the
  // backend rather than hardcoded here so the two never drift.
  snapshot_type_options: string[]
  run_type: RunType
  schedule_interval_unit: ScheduleIntervalUnit
  schedule_interval_value: number
  start_time: string
  next_run_time: string | null
  ticker_types: string | null
  tickers: string | null
  multiplier: number | null
  timespan: string | null
  backfill_days: number | null
  // Signed number of days from today (UTC) to sync bars through - e.g. 1 (the
  // default) for "through yesterday", 0 for "through today" - or null to default to 1
  // at run time. See backend-v2 jobs/sync_bars.py's sync_bars_nightly.
  bars_end_date_offset_days: number | null
  snapshot_types: string | null
  // ISO date ("YYYY-MM-DD"), or null to default to yesterday (UTC) at run time - see
  // backend-v2 jobs/average_volume.py's compute_average_volume.
  average_volume_start_date: string | null
  average_volume_days_interval: number | null
  // ISO dates ("YYYY-MM-DD"), or null to default to a trailing 90-day window ending
  // yesterday (UTC) at run time - see backend-v2 jobs/backtest_market_state.py's
  // compute_market_state_backtest.
  backtest_start_date: string | null
  backtest_end_date: string | null
  // ISO date ("YYYY-MM-DD"), or null to default to tomorrow (UTC) at run time - see
  // backend-v2 jobs/predict_market_state_10_day.py's compute_10_day_market_state_predictions.
  prediction_start_date: string | null
  // Signed number of days from today (UTC) - e.g. 1 for tomorrow, 0 for today, -1 for
  // yesterday - or null to default to tomorrow at run time. Only meaningful alongside
  // has_predicted_date_offset_field (the predict-market-state job): backend-v2
  // jobs/engine.py's run_job resolves this once per run into a concrete predicted
  // date and feeds it to both the Markov chain and (see mcmc_num_simulations below)
  // Monte Carlo phases of that job, in that order, so the two always target the same
  // session - see db/models.py's JobConfig.predicted_date_offset_days docstring.
  predicted_date_offset_days: number | null
  // Number of Monte Carlo paths simulated per ticker, or null to default to 2000 at
  // run time - see backend-v2 jobs/predict_market_state_mcmc.py's
  // compute_market_state_mcmc_predictions. Only meaningful alongside
  // has_monte_carlo_fields, the predict-market-state job's Monte Carlo phase (see
  // predicted_date_offset_days above for the date that phase shares with the Markov
  // chain phase before it).
  mcmc_num_simulations: number | null
  // Persisted (JobConfig.hidden), not display-only - keeps a job off the Jobs page's
  // default list across reloads until explicitly unhidden. Independent of running/
  // paused: a hidden job still runs on its schedule, it's just tucked away here.
  hidden: boolean
  running: boolean
  // Only meaningful while running - a job that isn't running can't be paused. Reflects
  // a pause *request*, not confirmation the run has actually parked at a checkpoint
  // (see backend-v2 jobs/control.py) - in practice that lag is well under a second.
  paused: boolean
  last_run: JobRun | null
}

export interface JobConfigInput {
  run_type: RunType
  schedule_interval_unit: ScheduleIntervalUnit
  schedule_interval_value: number
  start_time: string
  ticker_types?: string | null
  tickers?: string | null
  multiplier?: number | null
  timespan?: string | null
  backfill_days?: number | null
  bars_end_date_offset_days?: number | null
  snapshot_types?: string | null
  average_volume_start_date?: string | null
  average_volume_days_interval?: number | null
  backtest_start_date?: string | null
  backtest_end_date?: string | null
  prediction_start_date?: string | null
  predicted_date_offset_days?: number | null
  mcmc_num_simulations?: number | null
}

export interface TickerOption {
  ticker: string
  name: string | null
}

export interface TickerTypeOption {
  code: string
  asset_class: string
  description: string | null
}

// One row per (ticker, direction) from backend-v2's top_market_movers table, joined
// out to name/type - backs the Analytics > Top Movers report grid.
export interface TopMarketMoverRow {
  ticker: string
  name: string | null
  type: string | null
  asset_class: string | null
  average_volume: number | null
  market_cap: number | null
  direction: string
  rank: number
  todays_change: number | null
  todays_change_perc: number | null
  updated: string | null
  day_open: number | null
  day_high: number | null
  day_low: number | null
  day_close: number | null
  day_volume: number | null
  day_vwap: number | null
  min_open: number | null
  min_high: number | null
  min_low: number | null
  min_close: number | null
  min_volume: number | null
  min_vwap: number | null
  min_accumulated_volume: number | null
  min_timestamp: string | null
  prev_day_open: number | null
  prev_day_high: number | null
  prev_day_low: number | null
  prev_day_close: number | null
  prev_day_volume: number | null
  prev_day_vwap: number | null
  fetched_at: string
  // Fields below come from the ticker's most recent market_predictions row (jobs/predict_market_state.py) - all null if that job has never run for this ticker.
  predicted_date: string | null
  current_state: string | null
  predicted_state: string | null
  state_confidence: number | null
  expected_return: number | null
  // abs(expected_return) * 100 - derived client-side (see withAbsExpectedReturnPct
  // below) rather than by the backend, since it's purely a display transform of
  // expected_return, not new data.
  abs_expected_return_pct: number | null
  entry_price: number | null
  exit_price: number | null
  exit_price_confidence: number | null
  entry_time: string | null
  exit_time: string | null
  history_days: number | null
  prediction_computed_at: string | null
}

// One row per tickers row, joined out to asset_class/average_volume/current_snapshots -
// backs the Analytics > Trading Symbols report grid. Unlike TopMarketMoverRow, snapshot
// fields (everything past average_volume) can all be null - a ticker with no
// current_snapshots row (sync-snapshots never run against it) still gets a row here.
export interface TradingSymbolRow {
  ticker: string
  name: string | null
  type: string | null
  asset_class: string | null
  average_volume: number | null
  market_cap: number | null
  todays_change: number | null
  todays_change_perc: number | null
  updated: string | null
  day_open: number | null
  day_high: number | null
  day_low: number | null
  day_close: number | null
  day_volume: number | null
  day_vwap: number | null
  min_open: number | null
  min_high: number | null
  min_low: number | null
  min_close: number | null
  min_volume: number | null
  min_vwap: number | null
  min_accumulated_volume: number | null
  min_timestamp: string | null
  prev_day_open: number | null
  prev_day_high: number | null
  prev_day_low: number | null
  prev_day_close: number | null
  prev_day_volume: number | null
  prev_day_vwap: number | null
  fetched_at: string | null
  // Fields below come from the ticker's most recent market_predictions row (jobs/predict_market_state.py) - all null if that job has never run for this ticker.
  predicted_date: string | null
  current_state: string | null
  predicted_state: string | null
  state_confidence: number | null
  expected_return: number | null
  abs_expected_return_pct: number | null
  entry_price: number | null
  exit_price: number | null
  exit_price_confidence: number | null
  entry_time: string | null
  exit_time: string | null
  history_days: number | null
  prediction_computed_at: string | null
}

// Backend caps page_size at 1000 (see app/main.py's TRADING_SYMBOLS_MAX_PAGE_SIZE) -
// requesting more just gets clamped down to it server-side, not rejected.
export const TRADING_SYMBOLS_MAX_PAGE_SIZE = 1000

export interface TradingSymbolsPage {
  rows: TradingSymbolRow[]
  total: number
  page: number
  page_size: number
}

// Sort priority for the Trading Symbols report: applied in array order, so entry 0 is
// the primary sort key. `field` is one of TRADING_SYMBOLS_ORDERABLE_FIELDS' keys
// (see components/TradingSymbolsPage.tsx) - kept as a plain string here rather than a
// union so api.ts doesn't need to import the page's field list.
export interface TradingSymbolOrderField {
  field: string
  dir: 'asc' | 'desc'
}

// A real SQL WHERE, unlike ReportGrid's client-side numeric column filters - see
// app/main.py's trading_symbols_report. Both fields are required together; there's no
// "no filter" value here because the caller (TradingSymbolsPage) only constructs one of
// these once the operator and value are both set, passing `undefined` otherwise.
export interface NumericFilter {
  op: NumericFilterOp
  value: number
}

// One row per ticker whose most recent daily ohlc_bars row is stale or missing - backs
// the Analytics > Stale Tickers report grid (see app/main.py's stale_tickers_report).
// type_class/type_description are resolved from the ticker_types table the same
// first-seen-per-code way TradingSymbolRow's asset_class is.
export interface StaleTickerRow {
  ticker: string
  name: string | null
  type: string | null
  type_class: string | null
  type_description: string | null
  // ISO date ("YYYY-MM-DD"), or null if this ticker has never had a daily bar synced.
  last_ohlc_date: string | null
}

// Backend caps page_size at 1000 (see app/main.py's STALE_TICKERS_MAX_PAGE_SIZE).
export const STALE_TICKERS_MAX_PAGE_SIZE = 1000

export interface StaleTickersReport {
  rows: StaleTickerRow[]
  total: number
  page: number
  page_size: number
}

// Sort priority for the Stale Tickers report - same shape/reasoning as
// TradingSymbolOrderField, against STALE_TICKERS_ORDERABLE_FIELDS instead.
export interface StaleTickerOrderField {
  field: string
  dir: 'asc' | 'desc'
}

// One row per ticker's most recent market_predictions_10_day run (jobs/
// predict_market_state_10_day.py) - backs the Analytics > Next 10 Day Predictions
// report grid. Unlike TradingSymbolRow, a ticker with no 10-day prediction never
// appears here at all (see app/main.py's next_10_day_predictions_report) rather than
// showing up with every dayN_* field null.
export interface Next10DayPredictionRow {
  ticker: string
  name: string | null
  type: string | null
  asset_class: string | null
  average_volume: number | null
  market_cap: number | null
  // ISO date ("YYYY-MM-DD") - the prediction's own start date, not necessarily today.
  start_date: string
  current_state: string
  day1_predicted_state: string
  day1_state_confidence: number
  day1_entry_price: number
  day1_exit_price: number
  day1_expected_return_pct: number
  day1_entry_time: string
  day1_exit_time: string
  day2_predicted_state: string
  day2_state_confidence: number
  day2_entry_price: number
  day2_exit_price: number
  day2_expected_return_pct: number
  day2_entry_time: string
  day2_exit_time: string
  day3_predicted_state: string
  day3_state_confidence: number
  day3_entry_price: number
  day3_exit_price: number
  day3_expected_return_pct: number
  day3_entry_time: string
  day3_exit_time: string
  day4_predicted_state: string
  day4_state_confidence: number
  day4_entry_price: number
  day4_exit_price: number
  day4_expected_return_pct: number
  day4_entry_time: string
  day4_exit_time: string
  day5_predicted_state: string
  day5_state_confidence: number
  day5_entry_price: number
  day5_exit_price: number
  day5_expected_return_pct: number
  day5_entry_time: string
  day5_exit_time: string
  day6_predicted_state: string
  day6_state_confidence: number
  day6_entry_price: number
  day6_exit_price: number
  day6_expected_return_pct: number
  day6_entry_time: string
  day6_exit_time: string
  day7_predicted_state: string
  day7_state_confidence: number
  day7_entry_price: number
  day7_exit_price: number
  day7_expected_return_pct: number
  day7_entry_time: string
  day7_exit_time: string
  day8_predicted_state: string
  day8_state_confidence: number
  day8_entry_price: number
  day8_exit_price: number
  day8_expected_return_pct: number
  day8_entry_time: string
  day8_exit_time: string
  day9_predicted_state: string
  day9_state_confidence: number
  day9_entry_price: number
  day9_exit_price: number
  day9_expected_return_pct: number
  day9_entry_time: string
  day9_exit_time: string
  day10_predicted_state: string
  day10_state_confidence: number
  day10_entry_price: number
  day10_exit_price: number
  day10_expected_return_pct: number
  day10_entry_time: string
  day10_exit_time: string
  // ABS(exit - entry) * 100 / exit over the given day range - a non-negative
  // magnitude-of-move percentage normalized against the range's exit price, computed
  // server-side (see app/main.py's _next_10_day_prediction_fields), not a signed return.
  net_return_pct_days_1_5: number
  net_return_pct_days_6_10: number
  net_return_pct_days_1_10: number
  computed_at: string
}

// Backend caps page_size at 1000 (see app/main.py's NEXT_10_DAY_PREDICTIONS_MAX_PAGE_SIZE).
export const NEXT_10_DAY_PREDICTIONS_MAX_PAGE_SIZE = 1000

export interface Next10DayPredictionsReport {
  rows: Next10DayPredictionRow[]
  total: number
  page: number
  page_size: number
}

// Sort priority for the Next 10 Day Predictions report - same shape/reasoning as
// TradingSymbolOrderField, against NEXT_10_DAY_PREDICTIONS_ORDERABLE_FIELDS instead.
export interface Next10DayPredictionOrderField {
  field: string
  dir: 'asc' | 'desc'
}

// One row per (ticker, evaluated_date) from backend-v2's market_prediction_backtests
// table - backs the View Details chart's backtest-vs-actual price series.
export interface BacktestPoint {
  evaluated_date: string
  predicted_state: string
  actual_state: string
  predicted_correct: boolean
  expected_return: number
  entry_price: number
  predicted_exit_price: number
  actual_exit_price: number
  price_error_pct: number
}

// One row per ticker that has a Markov chain prediction (jobs/predict_market_state.py,
// markov_-prefixed) and/or a Monte Carlo prediction (jobs/predict_market_state_mcmc.py,
// mcmc_-prefixed) for the requested predicted_date - backs the Analytics > Market
// Predictions report grid (see app/main.py's market_predictions_report). A row needs
// only one of the two predictions to appear at all (the other side's markov_*/mcmc_*
// fields are null if that job didn't run for this ticker on this date) - unlike every
// other report's rows, average_volume/market_cap are the only fields guaranteed
// present regardless of which prediction(s) matched. predicted_date itself is never
// null - it's always exactly the date the report was run for, not per-prediction.
export interface MarketPredictionRow {
  ticker: string
  name: string | null
  average_volume: number | null
  market_cap: number | null
  // Backtest hit-rate (0-1 fraction of jobs/backtest_market_state.py's
  // predicted_correct, averaged across every evaluated_date on record for this
  // ticker) - null if the backtest job has never covered this ticker, not 0 (see
  // app/main.py's _BACKTEST_STATS_SUBQ).
  validation_score: number | null
  // Blended expected_return * exit_price_confidence, averaged across whichever of
  // markov_/mcmc_ this row actually has a prediction for (see app/main.py's
  // SURVIVOR_SCORE_EXPR / _survivor_score).
  survivor_score: number | null
  predicted_date: string
  markov_current_state: string | null
  markov_predicted_state: string | null
  markov_state_confidence: number | null
  markov_expected_return: number | null
  markov_entry_price: number | null
  markov_exit_price: number | null
  markov_exit_price_confidence: number | null
  markov_entry_time: string | null
  markov_exit_time: string | null
  markov_history_days: number | null
  markov_computed_at: string | null
  mcmc_current_state: string | null
  mcmc_predicted_state: string | null
  mcmc_state_confidence: number | null
  mcmc_expected_return: number | null
  mcmc_entry_price: number | null
  mcmc_exit_price: number | null
  mcmc_exit_price_mean: number | null
  mcmc_exit_price_std: number | null
  mcmc_exit_price_confidence: number | null
  mcmc_exit_price_p10: number | null
  mcmc_exit_price_p50: number | null
  mcmc_exit_price_p90: number | null
  mcmc_entry_time: string | null
  mcmc_exit_time: string | null
  mcmc_num_simulations: number | null
  mcmc_history_days: number | null
  mcmc_computed_at: string | null
}

// Backend caps page_size at 1000 (see app/main.py's MARKET_PREDICTIONS_MAX_PAGE_SIZE).
export const MARKET_PREDICTIONS_MAX_PAGE_SIZE = 1000

export interface MarketPredictionsReport {
  rows: MarketPredictionRow[]
  total: number
  page: number
  page_size: number
}

// Sort priority for the Market Predictions report - same shape/reasoning as
// TradingSymbolOrderField, against MARKET_PREDICTIONS_ORDERABLE_FIELDS instead.
export interface MarketPredictionOrderField {
  field: string
  dir: 'asc' | 'desc'
}

// Params for api.marketPredictionsReport - an options object rather than positional
// args (unlike tradingSymbolsReport/next10DayPredictionsReport above) because this
// report alone has a dozen-odd independent filters; a positional signature that long
// would be unreadable at the call site and error-prone to reorder. Every filter here
// maps 1:1 to one of app/main.py's market_predictions_report query params - see that
// function's docstring for what each one does and how markov_/mcmc_ filters combine
// (OR, each gating only its own side) vs. the global ones (average volume/market cap/
// validation/survivor score, require consensus - all AND'd on top).
export interface MarketPredictionsReportParams {
  predictedDate: string
  tickerTypes?: string[]
  tickers?: string[]
  page?: number
  pageSize?: number
  orderBy?: MarketPredictionOrderField[]
  markovExitPriceConfidenceFilter?: NumericFilter
  mcmcExitPriceConfidenceFilter?: NumericFilter
  averageVolumeFilter?: NumericFilter
  marketCapFilter?: NumericFilter
  markovHistoryDaysFilter?: NumericFilter
  mcmcHistoryDaysFilter?: NumericFilter
  markovStateConfidenceFilter?: NumericFilter
  mcmcStateConfidenceFilter?: NumericFilter
  markovExpectedReturnFilter?: NumericFilter
  mcmcExpectedReturnFilter?: NumericFilter
  markovPredictedStates?: string[]
  mcmcPredictedStates?: string[]
  requireConsensus?: boolean
  mcmcP10ReturnPctFilter?: NumericFilter
  validationScoreFilter?: NumericFilter
  survivorScoreFilter?: NumericFilter
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`)
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`)
  return res.json()
}

async function putJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => null)
    throw new Error(detail?.detail ?? `${path} failed: ${res.status}`)
  }
  return res.json()
}

async function postJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { method: 'POST' })
  if (!res.ok) {
    const detail = await res.json().catch(() => null)
    throw new Error(detail?.detail ?? `${path} failed: ${res.status}`)
  }
  return res.json()
}

// Fills in abs_expected_return_pct on a report row fetched from the backend, which only
// sends expected_return (a signed fraction, e.g. -0.29) - shared by both report fetches
// below so the derivation lives in one place.
function withAbsExpectedReturnPct<T extends { expected_return: number | null }>(
  row: T,
): T & { abs_expected_return_pct: number | null } {
  return {
    ...row,
    abs_expected_return_pct: row.expected_return == null ? null : Math.abs(row.expected_return) * 100,
  }
}

// Sets `${prefix}_op`/`${prefix}_value` on `qs` when `filter` is given, no-ops
// otherwise - shared by marketPredictionsReport's dozen-odd numeric filters below so
// each doesn't repeat this two-line ternary the way tradingSymbolsReport's three do.
function addNumericFilter(qs: URLSearchParams, prefix: string, filter?: NumericFilter): void {
  if (!filter) return
  qs.set(`${prefix}_op`, filter.op)
  qs.set(`${prefix}_value`, String(filter.value))
}

export type TriggerJobResult = { status: 'started' } | { status: 'already-running' }

export const api = {
  jobs: () => getJSON<Job[]>('/jobs'),
  job: (name: string) => getJSON<Job>(`/jobs/${name}`),
  jobRuns: (name: string, limit = 10) => getJSON<JobRun[]>(`/jobs/${name}/runs?limit=${limit}`),
  updateJobConfig: (name: string, config: JobConfigInput) => putJSON<Job>(`/jobs/${name}/config`, config),
  triggerJob: async (name: string): Promise<TriggerJobResult> => {
    const res = await fetch(`${API_BASE}/jobs/${name}/run`, { method: 'POST' })
    if (res.status === 409) return { status: 'already-running' }
    if (!res.ok) throw new Error(`trigger ${name} failed: ${res.status}`)
    return res.json()
  },
  pauseJob: (name: string) => postJSON<{ status: string }>(`/jobs/${name}/pause`),
  resumeJob: (name: string) => postJSON<{ status: string }>(`/jobs/${name}/resume`),
  cancelJob: (name: string) => postJSON<{ status: string }>(`/jobs/${name}/cancel`),
  hideJob: (name: string) => postJSON<Job>(`/jobs/${name}/hide`),
  unhideJob: (name: string) => postJSON<Job>(`/jobs/${name}/unhide`),
  resetJob: (name: string) => postJSON<Job>(`/jobs/${name}/reset`),
  searchTickers: (q: string, limit = 20) =>
    getJSON<TickerOption[]>(`/tickers/search?q=${encodeURIComponent(q)}&limit=${limit}`),
  searchTickerTypes: (q: string, limit = 20) =>
    getJSON<TickerTypeOption[]>(`/ticker-types/search?q=${encodeURIComponent(q)}&limit=${limit}`),
  topMoversReport: (tickerTypes: string[] = []) =>
    getJSON<TopMarketMoverRow[]>(`/reports/top-movers?ticker_types=${encodeURIComponent(tickerTypes.join(','))}`).then(
      (rows) => rows.map(withAbsExpectedReturnPct),
    ),
  tradingSymbolsReport: (
    tickerTypes: string[] = [],
    page = 1,
    pageSize = TRADING_SYMBOLS_MAX_PAGE_SIZE,
    orderBy: TradingSymbolOrderField[] = [],
    entryPriceFilter?: NumericFilter,
    marketCapFilter?: NumericFilter,
    tickers: string[] = [],
    predictedStates: string[] = [],
    stateConfidenceFilter?: NumericFilter,
  ) => {
    const orderByParam = orderBy.map(({ field, dir }) => `${field}:${dir}`).join(',')
    const entryPriceParam = entryPriceFilter
      ? `&entry_price_op=${encodeURIComponent(entryPriceFilter.op)}&entry_price_value=${entryPriceFilter.value}`
      : ''
    const marketCapParam = marketCapFilter
      ? `&market_cap_op=${encodeURIComponent(marketCapFilter.op)}&market_cap_value=${marketCapFilter.value}`
      : ''
    const stateConfidenceParam = stateConfidenceFilter
      ? `&state_confidence_op=${encodeURIComponent(stateConfidenceFilter.op)}&state_confidence_value=${stateConfidenceFilter.value}`
      : ''
    return getJSON<TradingSymbolsPage>(
      `/reports/trading-symbols?ticker_types=${encodeURIComponent(tickerTypes.join(','))}&tickers=${encodeURIComponent(tickers.join(','))}&page=${page}&page_size=${pageSize}&order_by=${encodeURIComponent(orderByParam)}${entryPriceParam}${marketCapParam}&predicted_states=${encodeURIComponent(predictedStates.join(','))}${stateConfidenceParam}`,
    ).then((data) => ({ ...data, rows: data.rows.map(withAbsExpectedReturnPct) }))
  },
  next10DayPredictionsReport: (
    tickerTypes: string[] = [],
    tickers: string[] = [],
    page = 1,
    pageSize = NEXT_10_DAY_PREDICTIONS_MAX_PAGE_SIZE,
    orderBy: Next10DayPredictionOrderField[] = [],
    marketCapFilter?: NumericFilter,
  ) => {
    const orderByParam = orderBy.map(({ field, dir }) => `${field}:${dir}`).join(',')
    const marketCapParam = marketCapFilter
      ? `&market_cap_op=${encodeURIComponent(marketCapFilter.op)}&market_cap_value=${marketCapFilter.value}`
      : ''
    return getJSON<Next10DayPredictionsReport>(
      `/reports/next-10-day-predictions?ticker_types=${encodeURIComponent(tickerTypes.join(','))}&tickers=${encodeURIComponent(tickers.join(','))}&page=${page}&page_size=${pageSize}&order_by=${encodeURIComponent(orderByParam)}${marketCapParam}`,
    )
  },
  marketPredictionsReport: (params: MarketPredictionsReportParams) => {
    const {
      predictedDate,
      tickerTypes = [],
      tickers = [],
      page = 1,
      pageSize = MARKET_PREDICTIONS_MAX_PAGE_SIZE,
      orderBy = [],
      markovExitPriceConfidenceFilter,
      mcmcExitPriceConfidenceFilter,
      averageVolumeFilter,
      marketCapFilter,
      markovHistoryDaysFilter,
      mcmcHistoryDaysFilter,
      markovStateConfidenceFilter,
      mcmcStateConfidenceFilter,
      markovExpectedReturnFilter,
      mcmcExpectedReturnFilter,
      markovPredictedStates = [],
      mcmcPredictedStates = [],
      requireConsensus = false,
      mcmcP10ReturnPctFilter,
      validationScoreFilter,
      survivorScoreFilter,
    } = params
    const qs = new URLSearchParams()
    qs.set('predicted_date', predictedDate)
    qs.set('ticker_types', tickerTypes.join(','))
    qs.set('tickers', tickers.join(','))
    qs.set('page', String(page))
    qs.set('page_size', String(pageSize))
    qs.set('order_by', orderBy.map(({ field, dir }) => `${field}:${dir}`).join(','))
    addNumericFilter(qs, 'markov_exit_price_confidence', markovExitPriceConfidenceFilter)
    addNumericFilter(qs, 'mcmc_exit_price_confidence', mcmcExitPriceConfidenceFilter)
    addNumericFilter(qs, 'average_volume', averageVolumeFilter)
    addNumericFilter(qs, 'market_cap', marketCapFilter)
    addNumericFilter(qs, 'markov_history_days', markovHistoryDaysFilter)
    addNumericFilter(qs, 'mcmc_history_days', mcmcHistoryDaysFilter)
    addNumericFilter(qs, 'markov_state_confidence', markovStateConfidenceFilter)
    addNumericFilter(qs, 'mcmc_state_confidence', mcmcStateConfidenceFilter)
    addNumericFilter(qs, 'markov_expected_return', markovExpectedReturnFilter)
    addNumericFilter(qs, 'mcmc_expected_return', mcmcExpectedReturnFilter)
    addNumericFilter(qs, 'mcmc_p10_return_pct', mcmcP10ReturnPctFilter)
    addNumericFilter(qs, 'validation_score', validationScoreFilter)
    addNumericFilter(qs, 'survivor_score', survivorScoreFilter)
    if (markovPredictedStates.length) qs.set('markov_predicted_states', markovPredictedStates.join(','))
    if (mcmcPredictedStates.length) qs.set('mcmc_predicted_states', mcmcPredictedStates.join(','))
    if (requireConsensus) qs.set('require_consensus', 'true')
    return getJSON<MarketPredictionsReport>(`/reports/market-predictions?${qs.toString()}`)
  },
  staleTickersReport: (
    tickerTypes: string[] = [],
    staleAfterDays = 1,
    page = 1,
    pageSize = STALE_TICKERS_MAX_PAGE_SIZE,
    orderBy: StaleTickerOrderField[] = [],
  ) => {
    const orderByParam = orderBy.map(({ field, dir }) => `${field}:${dir}`).join(',')
    return getJSON<StaleTickersReport>(
      `/reports/stale-tickers?ticker_types=${encodeURIComponent(tickerTypes.join(','))}&stale_after_days=${staleAfterDays}&page=${page}&page_size=${pageSize}&order_by=${encodeURIComponent(orderByParam)}`,
    )
  },
  // startDate/endDate are ISO dates ("YYYY-MM-DD"); omit either to let the backend
  // default to its trailing-15-day window ending yesterday (UTC) - see app/main.py's
  // backtest_report.
  backtestReport: (ticker: string, startDate?: string, endDate?: string) =>
    getJSON<BacktestPoint[]>(
      `/reports/backtest?ticker=${encodeURIComponent(ticker)}&start_date=${encodeURIComponent(startDate ?? '')}&end_date=${encodeURIComponent(endDate ?? '')}`,
    ),
}
