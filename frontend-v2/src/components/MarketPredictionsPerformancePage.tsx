import { useEffect, useRef, useState } from 'react'
import {
  api,
  MARKET_PREDICTIONS_PERFORMANCE_MAX_PAGE_SIZE,
  type MarketPredictionPerformanceOrderField,
  type MarketPredictionPerformanceRow,
  type TickerTypeOption,
} from '../api'
import { NUMERIC_FILTER_OPS, type NumericFilterOp } from '../numericFilter'
import { loadReportParams, saveReportParams } from '../reportParams'
import { ChevronIcon } from './icons'
import { ReportGrid, type ReportColumn } from './ReportGrid'
import { SearchableSelect, type SelectOption } from './SearchableSelect'

const REPORT_PARAMS_ID = 'market-predictions-performance'

type SavedParams = {
  startDate: string
  endDate: string
  tickerTypes: string[]
  tickers: string[]
  pageSize: number
  orderBy: MarketPredictionPerformanceOrderField[]
  marketCapOp: NumericFilterOp | ''
  marketCapValue: string
  markovExitPriceConfidenceOp: NumericFilterOp | ''
  markovExitPriceConfidenceValue: string
  mcmcExitPriceConfidenceOp: NumericFilterOp | ''
  mcmcExitPriceConfidenceValue: string
  markovWinRateOp: NumericFilterOp | ''
  markovWinRateValue: string
  mcmcWinRateOp: NumericFilterOp | ''
  mcmcWinRateValue: string
}

// Same reasoning as TradingSymbolsPage's TICKER_TYPE_OPTIONS_LIMIT.
const TICKER_TYPE_OPTIONS_LIMIT = 50

const DEFAULT_PAGE_SIZE = 500

// Fields the backend accepts in `order_by` (see
// MARKET_PREDICTIONS_PERFORMANCE_ORDERABLE_FIELDS in app/main.py) - every column
// COLUMNS below shows, since (unlike TradingSymbolsPage) this report has no page-scoped
// display-only fields to exclude.
const ORDER_BY_FIELDS: { key: string; label: string }[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'market', label: 'Market' },
  { key: 'locale', label: 'Locale' },
  { key: 'type', label: 'Type' },
  { key: 'description', label: 'Type Description' },
  { key: 'active', label: 'Active' },
  { key: 'currency_name', label: 'Currency' },
  { key: 'primary_exchange', label: 'Primary Exchange' },
  { key: 'market_cap', label: 'Market Cap' },
  { key: 'average_volume', label: 'Average Volume' },
  { key: 'predicted_date', label: 'Predicted Date' },
  { key: 'markov_current_state', label: 'Markov Current State' },
  { key: 'markov_predicted_state', label: 'Markov Predicted State' },
  { key: 'markov_state_confidence', label: 'Markov State Confidence' },
  { key: 'markov_expected_return', label: 'Markov Expected Return' },
  { key: 'markov_entry_price', label: 'Markov Entry Price' },
  { key: 'markov_exit_price', label: 'Markov Exit Price' },
  { key: 'markov_history_days', label: 'Markov History Days' },
  { key: 'markov_exit_price_confidence', label: 'Markov Exit Price Confidence' },
  { key: 'mcmc_current_state', label: 'MCMC Current State' },
  { key: 'mcmc_state_confidence', label: 'MCMC State Confidence' },
  { key: 'mcmc_expected_return', label: 'MCMC Expected Return' },
  { key: 'mcmc_entry_price', label: 'MCMC Entry Price' },
  { key: 'mcmc_exit_price', label: 'MCMC Exit Price' },
  { key: 'mcmc_history_days', label: 'MCMC History Days' },
  { key: 'mcmc_exit_price_confidence', label: 'MCMC Exit Price Confidence' },
  { key: 'actual_entry_price', label: 'Actual Entry Price' },
  { key: 'actual_exit_price', label: 'Actual Exit Price' },
  { key: 'actual_gain', label: 'Actual Gain' },
  { key: 'markov_result', label: 'Markov Result' },
  { key: 'mcmc_result', label: 'MCMC Result' },
  { key: 'mcmc_win_count', label: 'MCMC Win Count' },
  { key: 'mcmc_win_rate', label: 'MCMC Win Rate' },
  { key: 'mcmc_predictions_count', label: 'MCMC Predictions Count' },
  { key: 'markov_win_count', label: 'Markov Win Count' },
  { key: 'markov_win_rate', label: 'Markov Win Rate' },
  { key: 'markov_predictions_count', label: 'Markov Predictions Count' },
]

function tickerTypeLabel(t: TickerTypeOption): string {
  const detail = [t.asset_class, t.description].filter(Boolean).join(': ')
  return detail ? `${t.code} — ${detail}` : t.code
}

const COLUMNS: ReportColumn<MarketPredictionPerformanceRow>[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'market', label: 'Market' },
  { key: 'locale', label: 'Locale' },
  { key: 'type', label: 'Type' },
  { key: 'description', label: 'Type Description' },
  { key: 'active', label: 'Active' },
  { key: 'currency_name', label: 'Currency' },
  { key: 'primary_exchange', label: 'Primary Exchange' },
  { key: 'market_cap', label: 'Market Cap' },
  { key: 'average_volume', label: 'Average Volume' },
  { key: 'predicted_date', label: 'Predicted Date' },
  { key: 'markov_current_state', label: 'Markov Current State' },
  { key: 'markov_predicted_state', label: 'Markov Predicted State' },
  { key: 'markov_state_confidence', label: 'Markov State Confidence' },
  { key: 'markov_expected_return', label: 'Markov Expected Return' },
  { key: 'markov_entry_price', label: 'Markov Entry Price' },
  { key: 'markov_exit_price', label: 'Markov Exit Price' },
  { key: 'markov_history_days', label: 'Markov History Days' },
  { key: 'markov_exit_price_confidence', label: 'Markov Exit Price Confidence' },
  { key: 'mcmc_current_state', label: 'MCMC Current State' },
  { key: 'mcmc_state_confidence', label: 'MCMC State Confidence' },
  { key: 'mcmc_expected_return', label: 'MCMC Expected Return' },
  { key: 'mcmc_entry_price', label: 'MCMC Entry Price' },
  { key: 'mcmc_exit_price', label: 'MCMC Exit Price' },
  { key: 'mcmc_history_days', label: 'MCMC History Days' },
  { key: 'mcmc_exit_price_confidence', label: 'MCMC Exit Price Confidence' },
  { key: 'actual_entry_price', label: 'Actual Entry Price' },
  { key: 'actual_exit_price', label: 'Actual Exit Price' },
  { key: 'actual_gain', label: 'Actual Gain' },
  { key: 'markov_result', label: 'Markov Result' },
  { key: 'mcmc_result', label: 'MCMC Result' },
  { key: 'mcmc_win_count', label: 'MCMC Win Count' },
  { key: 'mcmc_win_rate', label: 'MCMC Win Rate' },
  { key: 'mcmc_predictions_count', label: 'MCMC Predictions Count' },
  { key: 'markov_win_count', label: 'Markov Win Count' },
  { key: 'markov_win_rate', label: 'Markov Win Rate' },
  { key: 'markov_predictions_count', label: 'Markov Predictions Count' },
]

const PERCENT_FIELDS = new Set<keyof MarketPredictionPerformanceRow>([
  'markov_win_rate',
  'mcmc_win_rate',
  'actual_gain',
])

function formatCell(row: MarketPredictionPerformanceRow, key: keyof MarketPredictionPerformanceRow): string {
  const value = row[key]
  if (value == null) return '–'
  if (key === 'active') return value ? 'Yes' : 'No'
  if (PERCENT_FIELDS.has(key)) return `${(Number(value) * (key === 'actual_gain' ? 1 : 100)).toFixed(2)}%`
  if (typeof value === 'number') return value.toLocaleString(undefined, { maximumFractionDigits: 4 })
  return String(value)
}

function rowKey(row: MarketPredictionPerformanceRow): string {
  return `${row.ticker}:${row.predicted_date}`
}

// Computed once on module load, not per-mount - same reasoning as
// TradingSymbolsPage's loadSavedParams.
function loadSavedParams(): Partial<SavedParams> | null {
  return loadReportParams<SavedParams>(REPORT_PARAMS_ID)
}

export function MarketPredictionsPerformancePage() {
  const [tickerTypeOptions, setTickerTypeOptions] = useState<SelectOption[]>([])
  const [startDate, setStartDate] = useState(() => loadSavedParams()?.startDate ?? '')
  const [endDate, setEndDate] = useState(() => loadSavedParams()?.endDate ?? '')
  const [tickerTypes, setTickerTypes] = useState<string[]>(() => loadSavedParams()?.tickerTypes ?? [])
  const [tickers, setTickers] = useState<string[]>(() => loadSavedParams()?.tickers ?? [])
  const [orderBy, setOrderBy] = useState<MarketPredictionPerformanceOrderField[]>(() => loadSavedParams()?.orderBy ?? [])
  // A real backend filter (see app/main.py's market_predictions_performance_report),
  // same shape/reasoning as TradingSymbolsPage's marketCapOp/marketCapValue.
  const [marketCapOp, setMarketCapOp] = useState<NumericFilterOp | ''>(() => loadSavedParams()?.marketCapOp ?? '')
  const [marketCapValue, setMarketCapValue] = useState(() => loadSavedParams()?.marketCapValue ?? '')
  const [markovExitPriceConfidenceOp, setMarkovExitPriceConfidenceOp] = useState<NumericFilterOp | ''>(
    () => loadSavedParams()?.markovExitPriceConfidenceOp ?? '',
  )
  const [markovExitPriceConfidenceValue, setMarkovExitPriceConfidenceValue] = useState(
    () => loadSavedParams()?.markovExitPriceConfidenceValue ?? '',
  )
  const [mcmcExitPriceConfidenceOp, setMcmcExitPriceConfidenceOp] = useState<NumericFilterOp | ''>(
    () => loadSavedParams()?.mcmcExitPriceConfidenceOp ?? '',
  )
  const [mcmcExitPriceConfidenceValue, setMcmcExitPriceConfidenceValue] = useState(
    () => loadSavedParams()?.mcmcExitPriceConfidenceValue ?? '',
  )
  const [markovWinRateOp, setMarkovWinRateOp] = useState<NumericFilterOp | ''>(
    () => loadSavedParams()?.markovWinRateOp ?? '',
  )
  const [markovWinRateValue, setMarkovWinRateValue] = useState(() => loadSavedParams()?.markovWinRateValue ?? '')
  const [mcmcWinRateOp, setMcmcWinRateOp] = useState<NumericFilterOp | ''>(
    () => loadSavedParams()?.mcmcWinRateOp ?? '',
  )
  const [mcmcWinRateValue, setMcmcWinRateValue] = useState(() => loadSavedParams()?.mcmcWinRateValue ?? '')
  // Not persisted - purely a display preference for the current visit, same reasoning
  // as JobCard's per-card collapsed state.
  const [filtersCollapsed, setFiltersCollapsed] = useState(false)
  const [pageSizeInput, setPageSizeInput] = useState(() => loadSavedParams()?.pageSize ?? DEFAULT_PAGE_SIZE)
  const [page, setPage] = useState(1)
  const [pageInput, setPageInput] = useState(1)
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [total, setTotal] = useState(0)
  const [rows, setRows] = useState<MarketPredictionPerformanceRow[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .searchTickerTypes('', TICKER_TYPE_OPTIONS_LIMIT)
      .then((matches) => setTickerTypeOptions(matches.map((t) => ({ value: t.code, label: tickerTypeLabel(t) }))))
  }, [])

  // undefined (not sent to the backend at all) until both an operator is chosen and a
  // parseable value typed - same reasoning as TradingSymbolsPage's marketCapFilter.
  const marketCapFilter =
    marketCapOp && marketCapValue.trim() !== '' && Number.isFinite(Number(marketCapValue))
      ? { op: marketCapOp, value: Number(marketCapValue) }
      : undefined
  const markovExitPriceConfidenceFilter =
    markovExitPriceConfidenceOp && markovExitPriceConfidenceValue.trim() !== '' && Number.isFinite(Number(markovExitPriceConfidenceValue))
      ? { op: markovExitPriceConfidenceOp, value: Number(markovExitPriceConfidenceValue) }
      : undefined
  const mcmcExitPriceConfidenceFilter =
    mcmcExitPriceConfidenceOp && mcmcExitPriceConfidenceValue.trim() !== '' && Number.isFinite(Number(mcmcExitPriceConfidenceValue))
      ? { op: mcmcExitPriceConfidenceOp, value: Number(mcmcExitPriceConfidenceValue) }
      : undefined
  const markovWinRateFilter =
    markovWinRateOp && markovWinRateValue.trim() !== '' && Number.isFinite(Number(markovWinRateValue))
      ? { op: markovWinRateOp, value: Number(markovWinRateValue) }
      : undefined
  const mcmcWinRateFilter =
    mcmcWinRateOp && mcmcWinRateValue.trim() !== '' && Number.isFinite(Number(mcmcWinRateValue))
      ? { op: mcmcWinRateOp, value: Number(mcmcWinRateValue) }
      : undefined

  const fetchPage = async (targetPage: number, requestedPageSize: number) => {
    setLoading(true)
    setError(null)
    try {
      const result = await api.marketPredictionsPerformanceReport(
        startDate || undefined,
        endDate || undefined,
        tickerTypes,
        tickers,
        targetPage,
        requestedPageSize,
        orderBy,
        marketCapFilter,
        markovExitPriceConfidenceFilter,
        mcmcExitPriceConfidenceFilter,
        markovWinRateFilter,
        mcmcWinRateFilter,
      )
      setRows(result.rows)
      setTotal(result.total)
      setPage(result.page)
      setPageInput(result.page)
      setPageSize(result.page_size)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load report')
    } finally {
      setLoading(false)
    }
  }

  const runReport = () => fetchPage(1, pageSizeInput)
  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  const [paramsJustSaved, setParamsJustSaved] = useState(false)
  const paramsSavedFlashTimeout = useRef<number | null>(null)
  useEffect(() => () => {
    if (paramsSavedFlashTimeout.current) window.clearTimeout(paramsSavedFlashTimeout.current)
  }, [])
  const saveParams = () => {
    saveReportParams<SavedParams>(REPORT_PARAMS_ID, {
      startDate,
      endDate,
      tickerTypes,
      tickers,
      pageSize: pageSizeInput,
      orderBy,
      marketCapOp,
      marketCapValue,
      markovExitPriceConfidenceOp,
      markovExitPriceConfidenceValue,
      mcmcExitPriceConfidenceOp,
      mcmcExitPriceConfidenceValue,
      markovWinRateOp,
      markovWinRateValue,
      mcmcWinRateOp,
      mcmcWinRateValue,
    })
    setParamsJustSaved(true)
    if (paramsSavedFlashTimeout.current) window.clearTimeout(paramsSavedFlashTimeout.current)
    paramsSavedFlashTimeout.current = window.setTimeout(() => setParamsJustSaved(false), 1500)
  }

  const addOrderField = (field: string) => {
    if (!field || orderBy.some((entry) => entry.field === field)) return
    setOrderBy([...orderBy, { field, dir: 'asc' }])
  }
  const removeOrderField = (index: number) => setOrderBy(orderBy.filter((_, i) => i !== index))
  const toggleOrderDir = (index: number) =>
    setOrderBy(orderBy.map((entry, i) => (i === index ? { ...entry, dir: entry.dir === 'asc' ? 'desc' : 'asc' } : entry)))
  const moveOrderField = (index: number, delta: number) => {
    const target = index + delta
    if (target < 0 || target >= orderBy.length) return
    const next = [...orderBy]
    ;[next[index], next[target]] = [next[target], next[index]]
    setOrderBy(next)
  }

  const goToPage = () => {
    if (!Number.isFinite(pageInput)) {
      setPageInput(page)
      return
    }
    const clamped = Math.min(totalPages, Math.max(1, Math.round(pageInput)))
    setPageInput(clamped)
    if (clamped !== page) fetchPage(clamped, pageSize)
  }

  return (
    <div className="report-page">
      <h1 className="jobs-page-title">Market Prediction Performance</h1>
      <p className="jobs-page-subtitle">
        Every market prediction in the chosen date range scored against what actually happened, alongside each
        ticker's running win rate. Leave both dates blank to default to today. Optionally filter by ticker type or
        specific tickers before running.
      </p>

      <div className="report-controls">
        <button
          type="button"
          className="report-filters-toggle"
          aria-expanded={!filtersCollapsed}
          onClick={() => setFiltersCollapsed((value) => !value)}
        >
          <ChevronIcon className={`icon${filtersCollapsed ? ' job-collapse-icon-collapsed' : ''}`} />
          <span>Filters</span>
        </button>
        {!filtersCollapsed && (
          <div className="report-controls-fields market-predictions-performance-fields">
            <div className="job-field">
              <span className="job-field-label">Start date</span>
              {/* No max=endDate here - that cross-cap disabled today whenever a stale saved endDate was in the past; start>end is caught server-side instead. */}
              <input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} />
            </div>
            <div className="job-field">
              <span className="job-field-label">End date</span>
              <input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} />
            </div>
            <div className="job-field report-ticker-type-field">
              <span className="job-field-label">Ticker types</span>
              <SearchableSelect
                multiple
                selected={tickerTypes}
                onChange={setTickerTypes}
                options={tickerTypeOptions}
                placeholder="Search ticker types... (leave blank for all)"
              />
            </div>
            <div className="job-field report-ticker-type-field">
              <span className="job-field-label">Tickers</span>
              <SearchableSelect
                multiple
                selected={tickers}
                onChange={setTickers}
                onSearch={(q) =>
                  api
                    .searchTickers(q)
                    .then((matches) => matches.map((t) => ({ value: t.ticker, label: t.name ? `${t.ticker} — ${t.name}` : t.ticker })))
                }
                placeholder="Search tickers... (leave blank for all)"
              />
            </div>
            <div className="job-field report-numeric-filter-field">
              <span className="job-field-label">Markov exit price confidence</span>
              <div className="report-numeric-filter-inputs">
                <select
                  value={markovExitPriceConfidenceOp}
                  onChange={(event) => setMarkovExitPriceConfidenceOp(event.target.value as NumericFilterOp | '')}
                >
                  <option value="">Any</option>
                  {NUMERIC_FILTER_OPS.map((op) => (
                    <option key={op} value={op}>
                      {op}
                    </option>
                  ))}
                </select>
                <input
                  type="number"
                  placeholder="Value"
                  value={markovExitPriceConfidenceValue}
                  onChange={(event) => setMarkovExitPriceConfidenceValue(event.target.value)}
                />
              </div>
            </div>
            <div className="job-field report-numeric-filter-field">
              <span className="job-field-label">MCMC exit price confidence</span>
              <div className="report-numeric-filter-inputs">
                <select
                  value={mcmcExitPriceConfidenceOp}
                  onChange={(event) => setMcmcExitPriceConfidenceOp(event.target.value as NumericFilterOp | '')}
                >
                  <option value="">Any</option>
                  {NUMERIC_FILTER_OPS.map((op) => (
                    <option key={op} value={op}>
                      {op}
                    </option>
                  ))}
                </select>
                <input
                  type="number"
                  placeholder="Value"
                  value={mcmcExitPriceConfidenceValue}
                  onChange={(event) => setMcmcExitPriceConfidenceValue(event.target.value)}
                />
              </div>
            </div>
            <div className="job-field report-numeric-filter-field">
              <span className="job-field-label">Markov win rate</span>
              <div className="report-numeric-filter-inputs">
                <select value={markovWinRateOp} onChange={(event) => setMarkovWinRateOp(event.target.value as NumericFilterOp | '')}>
                  <option value="">Any</option>
                  {NUMERIC_FILTER_OPS.map((op) => (
                    <option key={op} value={op}>
                      {op}
                    </option>
                  ))}
                </select>
                <input
                  type="number"
                  placeholder="Value"
                  value={markovWinRateValue}
                  onChange={(event) => setMarkovWinRateValue(event.target.value)}
                />
              </div>
            </div>
            <div className="job-field report-numeric-filter-field">
              <span className="job-field-label">MCMC win rate</span>
              <div className="report-numeric-filter-inputs">
                <select value={mcmcWinRateOp} onChange={(event) => setMcmcWinRateOp(event.target.value as NumericFilterOp | '')}>
                  <option value="">Any</option>
                  {NUMERIC_FILTER_OPS.map((op) => (
                    <option key={op} value={op}>
                      {op}
                    </option>
                  ))}
                </select>
                <input
                  type="number"
                  placeholder="Value"
                  value={mcmcWinRateValue}
                  onChange={(event) => setMcmcWinRateValue(event.target.value)}
                />
              </div>
            </div>
            <div className="job-field report-numeric-filter-field">
              <span className="job-field-label">Market cap</span>
              <div className="report-numeric-filter-inputs">
                <select value={marketCapOp} onChange={(event) => setMarketCapOp(event.target.value as NumericFilterOp | '')}>
                  <option value="">Any</option>
                  {NUMERIC_FILTER_OPS.map((op) => (
                    <option key={op} value={op}>
                      {op}
                    </option>
                  ))}
                </select>
                <input
                  type="number"
                  placeholder="Value"
                  value={marketCapValue}
                  onChange={(event) => setMarketCapValue(event.target.value)}
                />
              </div>
            </div>
            <div className="job-field report-page-size-field">
              <span className="job-field-label">Page size</span>
              <input
                type="number"
                min={1}
                max={MARKET_PREDICTIONS_PERFORMANCE_MAX_PAGE_SIZE}
                step={1}
                value={pageSizeInput}
                onChange={(event) => setPageSizeInput(Number(event.target.value))}
                onBlur={() =>
                  setPageSizeInput((current) =>
                    Number.isFinite(current)
                      ? Math.min(MARKET_PREDICTIONS_PERFORMANCE_MAX_PAGE_SIZE, Math.max(1, Math.round(current)))
                      : DEFAULT_PAGE_SIZE,
                  )
                }
              />
            </div>
            <div className="job-field report-order-by-field">
              <span className="job-field-label">Order by</span>
              <div className="order-by-picker">
                {orderBy.length > 0 && (
                  <ul className="order-by-list">
                    {orderBy.map((entry, index) => {
                      const field = ORDER_BY_FIELDS.find((f) => f.key === entry.field)
                      return (
                        <li key={entry.field} className="order-by-row">
                          <span className="order-by-priority">{index + 1}</span>
                          <span className="order-by-label">{field?.label ?? entry.field}</span>
                          <button
                            type="button"
                            className="order-by-dir-toggle"
                            onClick={() => toggleOrderDir(index)}
                            title={entry.dir === 'asc' ? 'Ascending - click for descending' : 'Descending - click for ascending'}
                          >
                            {entry.dir === 'asc' ? '▲ Asc' : '▼ Desc'}
                          </button>
                          <button
                            type="button"
                            className="order-by-move"
                            disabled={index === 0}
                            onClick={() => moveOrderField(index, -1)}
                            title="Move up in sort priority"
                            aria-label="Move up in sort priority"
                          >
                            ↑
                          </button>
                          <button
                            type="button"
                            className="order-by-move"
                            disabled={index === orderBy.length - 1}
                            onClick={() => moveOrderField(index, 1)}
                            title="Move down in sort priority"
                            aria-label="Move down in sort priority"
                          >
                            ↓
                          </button>
                          <button
                            type="button"
                            className="order-by-remove"
                            onClick={() => removeOrderField(index)}
                            title="Remove from sort"
                            aria-label="Remove from sort"
                          >
                            ×
                          </button>
                        </li>
                      )
                    })}
                  </ul>
                )}
                <select
                  className="order-by-add"
                  value=""
                  onChange={(event) => addOrderField(event.target.value)}
                  disabled={orderBy.length === ORDER_BY_FIELDS.length}
                >
                  <option value="" disabled>
                    {orderBy.length === ORDER_BY_FIELDS.length ? 'All fields added' : '+ Add sort field...'}
                  </option>
                  {ORDER_BY_FIELDS.filter((f) => !orderBy.some((entry) => entry.field === f.key)).map((f) => (
                    <option key={f.key} value={f.key}>
                      {f.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          </div>
        )}
        <div className="report-controls-actions">
          <button
            type="button"
            className="job-button job-button-primary"
            disabled={loading || !Number.isFinite(pageSizeInput) || pageSizeInput < 1}
            onClick={runReport}
          >
            {loading ? 'Running...' : 'Run report'}
          </button>
          <button type="button" className="job-button" onClick={saveParams}>
            {paramsJustSaved ? 'Saved' : 'Save parameters'}
          </button>
        </div>
      </div>

      {error && <p className="jobs-error">{error}</p>}

      {!loading && rows && (
        <>
          <ReportGrid
            columns={COLUMNS}
            rows={rows}
            rowKey={rowKey}
            formatCell={formatCell}
            emptyMessage="No market predictions found for this range."
            storageKey="market-predictions-performance"
          />
          <div className="report-pager">
            <button
              type="button"
              className="job-button"
              disabled={loading || page <= 1}
              onClick={() => fetchPage(page - 1, pageSize)}
            >
              Previous
            </button>
            <span className="report-pager-status">
              Page{' '}
              <input
                type="number"
                className="report-page-jump-input"
                min={1}
                max={totalPages}
                step={1}
                value={pageInput}
                disabled={loading}
                onChange={(event) => setPageInput(Number(event.target.value))}
                onBlur={goToPage}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    goToPage()
                  }
                }}
              />{' '}
              of {totalPages} ({total.toLocaleString()} rows)
            </span>
            <button
              type="button"
              className="job-button"
              disabled={loading || page >= totalPages}
              onClick={() => fetchPage(page + 1, pageSize)}
            >
              Next
            </button>
          </div>
        </>
      )}

      {!rows && !loading && !error && (
        <p className="placeholder-note">Choose a date range (optional) and run the report.</p>
      )}
    </div>
  )
}
