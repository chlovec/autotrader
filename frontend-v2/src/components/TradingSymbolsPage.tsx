import { useEffect, useRef, useState } from 'react'
import {
  api,
  TRADING_SYMBOLS_MAX_PAGE_SIZE,
  type TickerTypeOption,
  type TradingSymbolOrderField,
  type TradingSymbolRow,
} from '../api'
import { NUMERIC_FILTER_OPS, type NumericFilterOp } from '../numericFilter'
import { loadReportParams, saveReportParams } from '../reportParams'
import { ReportGrid, type ReportColumn } from './ReportGrid'
import { SearchableSelect, type SelectOption } from './SearchableSelect'
import { TickerDetailsModal } from './TickerDetailsModal'

const REPORT_PARAMS_ID = 'trading-symbols'

type SavedParams = {
  tickerTypes: string[]
  pageSize: number
  orderBy: TradingSymbolOrderField[]
  entryPriceOp: NumericFilterOp | ''
  entryPriceValue: string
}

// Backend caps /ticker-types/search's limit at 50 (see app/main.py's search_ticker_types) -
// ticker_types is a short, mostly-static reference list (db/models.py's TickerType
// docstring), so that's comfortably the whole thing in one page.
const TICKER_TYPE_OPTIONS_LIMIT = 50

const DEFAULT_PAGE_SIZE = 500

// Fields the backend accepts in `order_by` (see TRADING_SYMBOLS_ORDERABLE_FIELDS in
// app/main.py) - a subset of COLUMNS below, since ordering runs as a SQL ORDER BY
// against the full filtered set (not just the fetched page), so it's limited to
// fields that are cheap to sort on at the database level.
const ORDER_BY_FIELDS: { key: string; label: string }[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'type', label: 'Type' },
  { key: 'todays_change_perc', label: "Today's Change %" },
  { key: 'day_volume', label: 'Day Volume' },
  { key: 'abs_expected_return_pct', label: 'Abs Expected Return %' },
]

function tickerTypeLabel(t: TickerTypeOption): string {
  const detail = [t.asset_class, t.description].filter(Boolean).join(': ')
  return detail ? `${t.code} — ${detail}` : t.code
}

const COLUMNS: ReportColumn<TradingSymbolRow>[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'type', label: 'Type' },
  { key: 'asset_class', label: 'Asset Class' },
  { key: 'average_volume', label: 'Average Volume' },
  { key: 'market_cap', label: 'Market Cap' },
  { key: 'todays_change', label: "Today's Change" },
  { key: 'todays_change_perc', label: "Today's Change %" },
  { key: 'updated', label: 'Updated' },
  { key: 'day_open', label: 'Day Open' },
  { key: 'day_high', label: 'Day High' },
  { key: 'day_low', label: 'Day Low' },
  { key: 'day_close', label: 'Day Close' },
  { key: 'day_volume', label: 'Day Volume' },
  { key: 'day_vwap', label: 'Day VWAP' },
  { key: 'min_open', label: 'Min Open' },
  { key: 'min_high', label: 'Min High' },
  { key: 'min_low', label: 'Min Low' },
  { key: 'min_close', label: 'Min Close' },
  { key: 'min_volume', label: 'Min Volume' },
  { key: 'min_vwap', label: 'Min VWAP' },
  { key: 'min_accumulated_volume', label: 'Min Accumulated Volume' },
  { key: 'min_timestamp', label: 'Min Timestamp' },
  { key: 'prev_day_open', label: 'Prev Day Open' },
  { key: 'prev_day_high', label: 'Prev Day High' },
  { key: 'prev_day_low', label: 'Prev Day Low' },
  { key: 'prev_day_close', label: 'Prev Day Close' },
  { key: 'prev_day_volume', label: 'Prev Day Volume' },
  { key: 'prev_day_vwap', label: 'Prev Day VWAP' },
  { key: 'fetched_at', label: 'Fetched At' },
  { key: 'predicted_date', label: 'Predicted Date' },
  { key: 'current_state', label: 'Current State' },
  { key: 'predicted_state', label: 'Predicted State' },
  { key: 'state_confidence', label: 'State Confidence' },
  { key: 'expected_return', label: 'Expected Return' },
  { key: 'abs_expected_return_pct', label: 'Abs Expected Return %' },
  { key: 'entry_price', label: 'Entry Price' },
  { key: 'exit_price', label: 'Exit Price' },
  { key: 'entry_time', label: 'Entry Time' },
  { key: 'exit_time', label: 'Exit Time' },
  { key: 'history_days', label: 'History Days' },
  { key: 'prediction_computed_at', label: 'Prediction Computed At' },
]

// updated/min_timestamp/fetched_at/prediction_computed_at are naive-UTC (same as
// JobRun.started_at) - append "Z" so Date parses them as UTC instead of local time,
// same reasoning as JobCard's formatTimestamp. predicted_date is a plain date (no
// time component), so it's left out of this set and rendered as-is by formatCell.
const TIMESTAMP_FIELDS = new Set<keyof TradingSymbolRow>([
  'updated',
  'min_timestamp',
  'fetched_at',
  'prediction_computed_at',
])

function formatCell(row: TradingSymbolRow, key: keyof TradingSymbolRow): string {
  const value = row[key]
  if (value == null) return '–'
  if (TIMESTAMP_FIELDS.has(key)) return new Date(`${value}Z`).toLocaleString()
  if (key === 'abs_expected_return_pct') return `${(value as number).toFixed(2)}%`
  if (typeof value === 'number') return value.toLocaleString(undefined, { maximumFractionDigits: 4 })
  return String(value)
}

function rowKey(row: TradingSymbolRow): string {
  return row.ticker
}

// Computed once on module load (not per-mount) purely so the lazy useState
// initializers below - which all need the same saved blob - don't each re-read and
// re-parse localStorage independently.
function loadSavedParams(): Partial<SavedParams> | null {
  return loadReportParams<SavedParams>(REPORT_PARAMS_ID)
}

export function TradingSymbolsPage() {
  const [tickerTypeOptions, setTickerTypeOptions] = useState<SelectOption[]>([])
  const [tickerTypes, setTickerTypes] = useState<string[]>(() => loadSavedParams()?.tickerTypes ?? [])
  // Sort priority for the report - array order is priority order (index 0 = primary
  // sort key), same convention as SearchableSelect's chip order. Sent to the backend
  // as-is on the next fetch, same as tickerTypes below.
  const [orderBy, setOrderBy] = useState<TradingSymbolOrderField[]>(() => loadSavedParams()?.orderBy ?? [])
  // A real backend filter (see app/main.py's trading_symbols_report), unlike
  // ReportGrid's client-side numeric column filters - '' means "no operator chosen
  // yet", distinct from a chosen operator with a blank/unparseable value (see
  // entryPriceFilter below), so the two inputs can be edited independently without one
  // half silently clearing the other.
  const [entryPriceOp, setEntryPriceOp] = useState<NumericFilterOp | ''>(() => loadSavedParams()?.entryPriceOp ?? '')
  const [entryPriceValue, setEntryPriceValue] = useState(() => loadSavedParams()?.entryPriceValue ?? '')
  // Draft value bound to the page-size input, distinct from `pageSize` below (the
  // value the currently-displayed page was actually fetched with) - editing this
  // doesn't affect what's on screen, or what Prev/Next page through, until "Run
  // report" is clicked again.
  const [pageSizeInput, setPageSizeInput] = useState(() => loadSavedParams()?.pageSize ?? DEFAULT_PAGE_SIZE)
  const [page, setPage] = useState(1)
  // Draft value bound to the "Page X of N" jump-to-page input - same pattern as
  // pageSizeInput, but synced back to `page` on every successful fetch (including
  // Prev/Next) so it doesn't go stale sitting there mid-browse.
  const [pageInput, setPageInput] = useState(1)
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [total, setTotal] = useState(0)
  const [rows, setRows] = useState<TradingSymbolRow[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [detailsRow, setDetailsRow] = useState<TradingSymbolRow | null>(null)

  // Fetched once as a static list (rather than SearchableSelect's onSearch, which only
  // queries once the user types) so the dropdown opens showing every ticker type right
  // away - client-side filtered from there as the user types, same component.
  useEffect(() => {
    api
      .searchTickerTypes('', TICKER_TYPE_OPTIONS_LIMIT)
      .then((matches) => setTickerTypeOptions(matches.map((t) => ({ value: t.code, label: tickerTypeLabel(t) }))))
  }, [])

  // undefined (not sent to the backend at all) until both an operator is chosen and a
  // parseable value typed - matches ColumnHeaderMenu's numeric condition rows, where a
  // half-filled row is treated as not-yet-active rather than an error.
  const entryPriceFilter =
    entryPriceOp && entryPriceValue.trim() !== '' && Number.isFinite(Number(entryPriceValue))
      ? { op: entryPriceOp, value: Number(entryPriceValue) }
      : undefined

  const fetchPage = async (targetPage: number, requestedPageSize: number) => {
    setLoading(true)
    setError(null)
    try {
      const result = await api.tradingSymbolsReport(tickerTypes, targetPage, requestedPageSize, orderBy, entryPriceFilter)
      setRows(result.rows)
      setTotal(result.total)
      setPage(result.page)
      setPageInput(result.page)
      // Reflects back whatever the backend actually used (it clamps page_size too) -
      // Prev/Next below page off of this, not pageSizeInput, so mid-browse edits to
      // the input can't desync the offset math from what's actually on screen.
      setPageSize(result.page_size)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load report')
    } finally {
      setLoading(false)
    }
  }

  const runReport = () => fetchPage(1, pageSizeInput)
  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  // Persists the current draft controls (not necessarily the ones the on-screen page
  // was fetched with) so the next visit to this page restores them - separate from
  // ReportGrid's own "Save view", which only covers sort/filter/freeze/hide/width on
  // the grid itself, not these report-level controls.
  const [paramsJustSaved, setParamsJustSaved] = useState(false)
  const paramsSavedFlashTimeout = useRef<number | null>(null)
  useEffect(() => () => {
    if (paramsSavedFlashTimeout.current) window.clearTimeout(paramsSavedFlashTimeout.current)
  }, [])
  const saveParams = () => {
    saveReportParams<SavedParams>(REPORT_PARAMS_ID, {
      tickerTypes,
      pageSize: pageSizeInput,
      orderBy,
      entryPriceOp,
      entryPriceValue,
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
      <h1 className="jobs-page-title">Trading Symbols</h1>
      <p className="jobs-page-subtitle">
        Every synced ticker with its reference data, average volume, and latest snapshot. Optionally filter by
        ticker type before running.
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
          <div className="job-field report-entry-price-field">
            <span className="job-field-label">Entry price</span>
            <div className="report-entry-price-inputs">
              <select value={entryPriceOp} onChange={(event) => setEntryPriceOp(event.target.value as NumericFilterOp | '')}>
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
                value={entryPriceValue}
                onChange={(event) => setEntryPriceValue(event.target.value)}
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
              max={TRADING_SYMBOLS_MAX_PAGE_SIZE}
              step={1}
              value={pageSizeInput}
              onChange={(event) => setPageSizeInput(Number(event.target.value))}
              onBlur={() =>
                setPageSizeInput((current) =>
                  Number.isFinite(current) ? Math.min(TRADING_SYMBOLS_MAX_PAGE_SIZE, Math.max(1, Math.round(current))) : DEFAULT_PAGE_SIZE,
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
            emptyMessage="No symbols found."
            storageKey="trading-symbols"
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
        <p className="placeholder-note">Choose ticker types and page size (optional) and run the report.</p>
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
