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
  snapshot_types?: string | null
  average_volume_start_date?: string | null
  average_volume_days_interval?: number | null
  backtest_start_date?: string | null
  backtest_end_date?: string | null
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
    getJSON<TopMarketMoverRow[]>(`/reports/top-movers?ticker_types=${encodeURIComponent(tickerTypes.join(','))}`),
  tradingSymbolsReport: (
    tickerTypes: string[] = [],
    page = 1,
    pageSize = TRADING_SYMBOLS_MAX_PAGE_SIZE,
    orderBy: TradingSymbolOrderField[] = [],
  ) => {
    const orderByParam = orderBy.map(({ field, dir }) => `${field}:${dir}`).join(',')
    return getJSON<TradingSymbolsPage>(
      `/reports/trading-symbols?ticker_types=${encodeURIComponent(tickerTypes.join(','))}&page=${page}&page_size=${pageSize}&order_by=${encodeURIComponent(orderByParam)}`,
    )
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
}
