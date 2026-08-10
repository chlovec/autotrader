import { useEffect, useRef, useState } from 'react'
import {
  api,
  NEXT_10_DAY_PREDICTIONS_MAX_PAGE_SIZE,
  type Next10DayPredictionOrderField,
  type Next10DayPredictionRow,
  type TickerTypeOption,
} from '../api'
import { NUMERIC_FILTER_OPS, type NumericFilterOp } from '../numericFilter'
import { loadReportParams, saveReportParams } from '../reportParams'
import { ReportGrid, type ReportColumn } from './ReportGrid'
import { SearchableSelect, type SelectOption } from './SearchableSelect'
import { TickerDetailsModal } from './TickerDetailsModal'

const REPORT_PARAMS_ID = 'next-10-day-predictions'

type SavedParams = {
  tickerTypes: string[]
  tickers: string[]
  pageSize: number
  orderBy: Next10DayPredictionOrderField[]
  marketCapOp: NumericFilterOp | ''
  marketCapValue: string
}

// Backend caps /ticker-types/search's limit at 50 (see app/main.py's search_ticker_types) -
// same reasoning as TradingSymbolsPage.
const TICKER_TYPE_OPTIONS_LIMIT = 50

const DEFAULT_PAGE_SIZE = 500

// Fields the backend accepts in `order_by` (see NEXT_10_DAY_PREDICTIONS_ORDERABLE_FIELDS
// in app/main.py).
const ORDER_BY_FIELDS: { key: string; label: string }[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'type', label: 'Type' },
  { key: 'net_return_pct_days_1_10', label: 'Net Return % (1-10 Days)' },
]

function tickerTypeLabel(t: TickerTypeOption): string {
  const detail = [t.asset_class, t.description].filter(Boolean).join(': ')
  return detail ? `${t.code} — ${detail}` : t.code
}

// One column entry per market_predictions_10_day field (see db/models.py's
// MarketPrediction10Day) plus the base ticker/reference columns and the 3 net-return
// summary fields - written out explicitly, same "no metaprogrammed columns" reasoning
// as db/models.py's own column definitions.
const COLUMNS: ReportColumn<Next10DayPredictionRow>[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'type', label: 'Type' },
  { key: 'asset_class', label: 'Asset Class' },
  { key: 'average_volume', label: 'Average Volume' },
  { key: 'market_cap', label: 'Market Cap' },
  { key: 'start_date', label: 'Start Date' },
  { key: 'current_state', label: 'Current State' },
  { key: 'day1_predicted_state', label: 'Day 1 Predicted State' },
  { key: 'day1_state_confidence', label: 'Day 1 State Confidence' },
  { key: 'day1_entry_price', label: 'Day 1 Entry Price' },
  { key: 'day1_exit_price', label: 'Day 1 Exit Price' },
  { key: 'day1_expected_return_pct', label: 'Day 1 Expected Return %' },
  { key: 'day1_entry_time', label: 'Day 1 Entry Time' },
  { key: 'day1_exit_time', label: 'Day 1 Exit Time' },
  { key: 'day2_predicted_state', label: 'Day 2 Predicted State' },
  { key: 'day2_state_confidence', label: 'Day 2 State Confidence' },
  { key: 'day2_entry_price', label: 'Day 2 Entry Price' },
  { key: 'day2_exit_price', label: 'Day 2 Exit Price' },
  { key: 'day2_expected_return_pct', label: 'Day 2 Expected Return %' },
  { key: 'day2_entry_time', label: 'Day 2 Entry Time' },
  { key: 'day2_exit_time', label: 'Day 2 Exit Time' },
  { key: 'day3_predicted_state', label: 'Day 3 Predicted State' },
  { key: 'day3_state_confidence', label: 'Day 3 State Confidence' },
  { key: 'day3_entry_price', label: 'Day 3 Entry Price' },
  { key: 'day3_exit_price', label: 'Day 3 Exit Price' },
  { key: 'day3_expected_return_pct', label: 'Day 3 Expected Return %' },
  { key: 'day3_entry_time', label: 'Day 3 Entry Time' },
  { key: 'day3_exit_time', label: 'Day 3 Exit Time' },
  { key: 'day4_predicted_state', label: 'Day 4 Predicted State' },
  { key: 'day4_state_confidence', label: 'Day 4 State Confidence' },
  { key: 'day4_entry_price', label: 'Day 4 Entry Price' },
  { key: 'day4_exit_price', label: 'Day 4 Exit Price' },
  { key: 'day4_expected_return_pct', label: 'Day 4 Expected Return %' },
  { key: 'day4_entry_time', label: 'Day 4 Entry Time' },
  { key: 'day4_exit_time', label: 'Day 4 Exit Time' },
  { key: 'day5_predicted_state', label: 'Day 5 Predicted State' },
  { key: 'day5_state_confidence', label: 'Day 5 State Confidence' },
  { key: 'day5_entry_price', label: 'Day 5 Entry Price' },
  { key: 'day5_exit_price', label: 'Day 5 Exit Price' },
  { key: 'day5_expected_return_pct', label: 'Day 5 Expected Return %' },
  { key: 'day5_entry_time', label: 'Day 5 Entry Time' },
  { key: 'day5_exit_time', label: 'Day 5 Exit Time' },
  { key: 'day6_predicted_state', label: 'Day 6 Predicted State' },
  { key: 'day6_state_confidence', label: 'Day 6 State Confidence' },
  { key: 'day6_entry_price', label: 'Day 6 Entry Price' },
  { key: 'day6_exit_price', label: 'Day 6 Exit Price' },
  { key: 'day6_expected_return_pct', label: 'Day 6 Expected Return %' },
  { key: 'day6_entry_time', label: 'Day 6 Entry Time' },
  { key: 'day6_exit_time', label: 'Day 6 Exit Time' },
  { key: 'day7_predicted_state', label: 'Day 7 Predicted State' },
  { key: 'day7_state_confidence', label: 'Day 7 State Confidence' },
  { key: 'day7_entry_price', label: 'Day 7 Entry Price' },
  { key: 'day7_exit_price', label: 'Day 7 Exit Price' },
  { key: 'day7_expected_return_pct', label: 'Day 7 Expected Return %' },
  { key: 'day7_entry_time', label: 'Day 7 Entry Time' },
  { key: 'day7_exit_time', label: 'Day 7 Exit Time' },
  { key: 'day8_predicted_state', label: 'Day 8 Predicted State' },
  { key: 'day8_state_confidence', label: 'Day 8 State Confidence' },
  { key: 'day8_entry_price', label: 'Day 8 Entry Price' },
  { key: 'day8_exit_price', label: 'Day 8 Exit Price' },
  { key: 'day8_expected_return_pct', label: 'Day 8 Expected Return %' },
  { key: 'day8_entry_time', label: 'Day 8 Entry Time' },
  { key: 'day8_exit_time', label: 'Day 8 Exit Time' },
  { key: 'day9_predicted_state', label: 'Day 9 Predicted State' },
  { key: 'day9_state_confidence', label: 'Day 9 State Confidence' },
  { key: 'day9_entry_price', label: 'Day 9 Entry Price' },
  { key: 'day9_exit_price', label: 'Day 9 Exit Price' },
  { key: 'day9_expected_return_pct', label: 'Day 9 Expected Return %' },
  { key: 'day9_entry_time', label: 'Day 9 Entry Time' },
  { key: 'day9_exit_time', label: 'Day 9 Exit Time' },
  { key: 'day10_predicted_state', label: 'Day 10 Predicted State' },
  { key: 'day10_state_confidence', label: 'Day 10 State Confidence' },
  { key: 'day10_entry_price', label: 'Day 10 Entry Price' },
  { key: 'day10_exit_price', label: 'Day 10 Exit Price' },
  { key: 'day10_expected_return_pct', label: 'Day 10 Expected Return %' },
  { key: 'day10_entry_time', label: 'Day 10 Entry Time' },
  { key: 'day10_exit_time', label: 'Day 10 Exit Time' },
  { key: 'net_return_pct_days_1_5', label: 'Net Return % (Days 1-5)' },
  { key: 'net_return_pct_days_6_10', label: 'Net Return % (Days 6-10)' },
  { key: 'net_return_pct_days_1_10', label: 'Net Return % (Days 1-10)' },
  { key: 'computed_at', label: 'Computed At' },
]

// computed_at is naive-UTC (same as JobRun.started_at) - append "Z" so Date parses it
// as UTC instead of local time, same reasoning as JobCard's formatTimestamp.
// start_date is a plain date (no time component), so it's left out of this set and
// rendered as-is by formatCell, same as TradingSymbolsPage's predicted_date.
const TIMESTAMP_FIELDS = new Set<keyof Next10DayPredictionRow>(['computed_at'])

// Every field this report reports as an actual percentage (server-computed, see
// app/main.py's _next_10_day_prediction_fields) - formatted with a trailing "%",
// same reasoning as TradingSymbolsPage's abs_expected_return_pct.
const PERCENT_FIELDS = new Set<keyof Next10DayPredictionRow>([
  'day1_expected_return_pct',
  'day2_expected_return_pct',
  'day3_expected_return_pct',
  'day4_expected_return_pct',
  'day5_expected_return_pct',
  'day6_expected_return_pct',
  'day7_expected_return_pct',
  'day8_expected_return_pct',
  'day9_expected_return_pct',
  'day10_expected_return_pct',
  'net_return_pct_days_1_5',
  'net_return_pct_days_6_10',
  'net_return_pct_days_1_10',
])

function formatCell(row: Next10DayPredictionRow, key: keyof Next10DayPredictionRow): string {
  const value = row[key]
  if (value == null) return '–'
  if (TIMESTAMP_FIELDS.has(key)) return new Date(`${value}Z`).toLocaleString()
  if (PERCENT_FIELDS.has(key)) return `${(value as number).toFixed(2)}%`
  if (typeof value === 'number') return value.toLocaleString(undefined, { maximumFractionDigits: 4 })
  return String(value)
}

function rowKey(row: Next10DayPredictionRow): string {
  return row.ticker
}

// Computed once on module load (not per-mount) - same reasoning as TradingSymbolsPage's
// loadSavedParams.
function loadSavedParams(): Partial<SavedParams> | null {
  return loadReportParams<SavedParams>(REPORT_PARAMS_ID)
}

export function Next10DayPredictionsPage() {
  const [tickerTypeOptions, setTickerTypeOptions] = useState<SelectOption[]>([])
  const [tickerTypes, setTickerTypes] = useState<string[]>(() => loadSavedParams()?.tickerTypes ?? [])
  const [tickers, setTickers] = useState<string[]>(() => loadSavedParams()?.tickers ?? [])
  const [orderBy, setOrderBy] = useState<Next10DayPredictionOrderField[]>(() => loadSavedParams()?.orderBy ?? [])
  const [marketCapOp, setMarketCapOp] = useState<NumericFilterOp | ''>(() => loadSavedParams()?.marketCapOp ?? '')
  const [marketCapValue, setMarketCapValue] = useState(() => loadSavedParams()?.marketCapValue ?? '')
  const [pageSizeInput, setPageSizeInput] = useState(() => loadSavedParams()?.pageSize ?? DEFAULT_PAGE_SIZE)
  const [page, setPage] = useState(1)
  const [pageInput, setPageInput] = useState(1)
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [total, setTotal] = useState(0)
  const [rows, setRows] = useState<Next10DayPredictionRow[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [detailsRow, setDetailsRow] = useState<Next10DayPredictionRow | null>(null)

  useEffect(() => {
    api
      .searchTickerTypes('', TICKER_TYPE_OPTIONS_LIMIT)
      .then((matches) => setTickerTypeOptions(matches.map((t) => ({ value: t.code, label: tickerTypeLabel(t) }))))
  }, [])

  const marketCapFilter =
    marketCapOp && marketCapValue.trim() !== '' && Number.isFinite(Number(marketCapValue))
      ? { op: marketCapOp, value: Number(marketCapValue) }
      : undefined

  const fetchPage = async (targetPage: number, requestedPageSize: number) => {
    setLoading(true)
    setError(null)
    try {
      const result = await api.next10DayPredictionsReport(
        tickerTypes,
        tickers,
        targetPage,
        requestedPageSize,
        orderBy,
        marketCapFilter,
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
      tickerTypes,
      tickers,
      pageSize: pageSizeInput,
      orderBy,
      marketCapOp,
      marketCapValue,
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
      <h1 className="jobs-page-title">Next 10 Day Predictions</h1>
      <p className="jobs-page-subtitle">
        Each ticker's most recent 10-trading-day-ahead projection (predict-10-day-market-state job). Optionally
        filter by ticker type, specific tickers, or market cap before running.
      </p>

      <div className="report-controls">
        <div className="report-controls-fields">
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
        </div>
        <div className="report-controls-fields">
          <div className="job-field report-page-size-field">
            <span className="job-field-label">Page size</span>
            <input
              type="number"
              min={1}
              max={NEXT_10_DAY_PREDICTIONS_MAX_PAGE_SIZE}
              step={1}
              value={pageSizeInput}
              onChange={(event) => setPageSizeInput(Number(event.target.value))}
              onBlur={() =>
                setPageSizeInput((current) =>
                  Number.isFinite(current)
                    ? Math.min(NEXT_10_DAY_PREDICTIONS_MAX_PAGE_SIZE, Math.max(1, Math.round(current)))
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
            emptyMessage="No predictions found."
            storageKey="next-10-day-predictions"
            rowContextMenu={[{ label: 'View Details', onSelect: setDetailsRow }]}
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
              of {totalPages} ({total.toLocaleString()} tickers)
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
        <p className="placeholder-note">Choose filters and page size (optional) and run the report.</p>
      )}

      {detailsRow && (
        <TickerDetailsModal
          row={detailsRow}
          title={`${detailsRow.ticker} - ${detailsRow.name ?? 'Details'}`}
          columns={COLUMNS}
          formatCell={formatCell}
          onClose={() => setDetailsRow(null)}
        />
      )}
    </div>
  )
}
