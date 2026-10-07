import { useEffect, useRef, useState } from 'react'
import {
  api,
  BUY_SELL_PATTERN_TICKERS_MAX_PAGE_SIZE,
  type BuySellPatternTickerRow,
  type Next10DayPredictionOrderField,
  type TickerTypeOption,
} from '../api'
import { loadReportParams, saveReportParams } from '../reportParams'
import { BuySellPatternModal } from './BuySellPatternModal'
import { ReportGrid, type ReportColumn } from './ReportGrid'
import { SearchableSelect, type SelectOption } from './SearchableSelect'

// Saved-params / grid-layout key per variant, so the two pages keep separate settings.
const REPORT_PARAMS_ID = 'buy-sell-pattern'
const RANGE_REPORT_PARAMS_ID = 'buy-sell-pattern-range'

type SavedParams = {
  patternName: string
  tickerTypes: string[]
  tickers: string[]
  pageSize: number
  orderBy: Next10DayPredictionOrderField[]
  // Date Range variant only.
  startDate?: string
  endDate?: string
}

// Same reasoning as TradingSymbolsPage's TICKER_TYPE_OPTIONS_LIMIT.
const TICKER_TYPE_OPTIONS_LIMIT = 50

const DEFAULT_PAGE_SIZE = 500

// Fields the backend accepts in `order_by` (see BUY_SELL_PATTERN_TICKERS_ORDERABLE_FIELDS
// in app/main.py).
const ORDER_BY_FIELDS: { key: string; label: string }[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'type', label: 'Ticker Type' },
  { key: 'avg_profit', label: 'Avg Profit %' },
]

function tickerTypeLabel(t: TickerTypeOption): string {
  const detail = [t.asset_class, t.description].filter(Boolean).join(': ')
  return detail ? `${t.code} — ${detail}` : t.code
}

const COLUMNS: ReportColumn<BuySellPatternTickerRow>[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'type', label: 'Type' },
  { key: 'primary_exchange', label: 'Exchange' },
  { key: 'latest_close', label: 'Latest Close' },
  { key: 'trades', label: 'Trades' },
  { key: 'total_profit', label: 'Total Profit %' },
  { key: 'avg_profit', label: 'Avg Profit %' },
  { key: 'first_trade_datetime', label: 'First Trade' },
  { key: 'last_trade_datetime', label: 'Last Trade' },
  { key: 'avg_buy_price', label: 'Avg Buy' },
  { key: 'buy_price_min', label: 'Buy Min' },
  { key: 'buy_price_max', label: 'Buy Max' },
  { key: 'buy_price_median', label: 'Buy Median' },
  { key: 'avg_sell_price', label: 'Avg Sell' },
  { key: 'sell_price_min', label: 'Sell Min' },
  { key: 'sell_price_max', label: 'Sell Max' },
  { key: 'sell_price_median', label: 'Sell Median' },
  { key: 'computed_at', label: 'Computed At' },
]

// first/last_trade_datetime are naive US/Eastern market time - shown as just their date
// ("YYYY-MM-DD"), never shifted to the browser's timezone. computed_at is naive UTC, so it
// gets the same "Z" append as Next10DayPredictionsPage's TIMESTAMP_FIELDS.
const MARKET_TIME_FIELDS = new Set<keyof BuySellPatternTickerRow>(['first_trade_datetime', 'last_trade_datetime'])

function formatCell(row: BuySellPatternTickerRow, key: keyof BuySellPatternTickerRow): string {
  const value = row[key]
  if (value == null) return ''
  if (MARKET_TIME_FIELDS.has(key)) return (value as string).slice(0, 10)
  if (key === 'computed_at') return new Date(`${value}Z`).toLocaleString()
  if (key === 'trades') return String(value)
  if (key === 'total_profit' || key === 'avg_profit') {
    return (value as number).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  }
  if (typeof value === 'number') return value.toFixed(2)
  return String(value)
}

function rowKey(row: BuySellPatternTickerRow): string {
  return row.ticker
}

function loadSavedParams(id: string): Partial<SavedParams> | null {
  return loadReportParams<SavedParams>(id)
}

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10)
}

// Date Range variant's default window: the last year, through today (UTC).
function defaultRange(): { start: string; end: string } {
  const end = new Date()
  const start = new Date(end)
  start.setUTCFullYear(end.getUTCFullYear() - 1)
  return { start: isoDate(start), end: isoDate(end) }
}

type BuySellPatternPageProps = {
  // false (default): the Buy Sell Pattern page, reading buy_sell_pattern_stats as of
  // the last buy-sell-pattern-stats run. true: the Buy Sell Pattern (Date Range) page,
  // which computes the same stats on the fly from buy_sell_patterns over only the
  // trades within a chosen start/end date.
  dateRange?: boolean
}

// Lists the tickers the buy-sell-pattern job (jobs/buy_sell_pattern.py) stored trades
// for under one pattern name, with a View button per row that opens the trades on a
// chart in BuySellPatternModal, where the date range can be narrowed.
export function BuySellPatternPage({ dateRange = false }: BuySellPatternPageProps) {
  const paramsId = dateRange ? RANGE_REPORT_PARAMS_ID : REPORT_PARAMS_ID
  // Read once per mount - each variant is its own page, so paramsId never changes.
  const [saved] = useState(() => loadSavedParams(paramsId))
  const [patternNames, setPatternNames] = useState<string[] | null>(null)
  const [patternName, setPatternName] = useState(() => saved?.patternName ?? '')
  const [startDate, setStartDate] = useState(() => saved?.startDate ?? defaultRange().start)
  const [endDate, setEndDate] = useState(() => saved?.endDate ?? defaultRange().end)
  const [tickerTypeOptions, setTickerTypeOptions] = useState<SelectOption[]>([])
  const [tickerTypes, setTickerTypes] = useState<string[]>(() => saved?.tickerTypes ?? [])
  const [tickers, setTickers] = useState<string[]>(() => saved?.tickers ?? [])
  const [orderBy, setOrderBy] = useState<Next10DayPredictionOrderField[]>(() => saved?.orderBy ?? [])
  const [pageSizeInput, setPageSizeInput] = useState(() => saved?.pageSize ?? DEFAULT_PAGE_SIZE)
  const [page, setPage] = useState(1)
  const [pageInput, setPageInput] = useState(1)
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [total, setTotal] = useState(0)
  const [rows, setRows] = useState<BuySellPatternTickerRow[] | null>(null)
  // The pattern the current rows belong to - the picker can change before the next run.
  const [rowsPatternName, setRowsPatternName] = useState('')
  // Likewise the date range the current rows were computed over (Date Range variant).
  const [rowsRange, setRowsRange] = useState<{ start: string; end: string } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [patternRow, setPatternRow] = useState<BuySellPatternTickerRow | null>(null)

  useEffect(() => {
    api
      .searchTickerTypes('', TICKER_TYPE_OPTIONS_LIMIT)
      .then((matches) => setTickerTypeOptions(matches.map((t) => ({ value: t.code, label: tickerTypeLabel(t) }))))
    ;(dateRange ? api.buySellPatternPatternNames() : api.buySellPatternNames())
      .then((names) => {
        setPatternNames(names)
        // Keep a saved pattern only if it still has rows; otherwise start on the first.
        setPatternName((current) => (names.includes(current) ? current : (names[0] ?? '')))
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load patterns'))
  }, [dateRange])

  const rangeError = !dateRange
    ? null
    : !startDate || !endDate
      ? 'Pick both a start and an end date.'
      : startDate > endDate
        ? 'Start date must not be after end date.'
        : null

  const fetchPage = async (targetPage: number, requestedPageSize: number) => {
    if (!patternName || rangeError) return
    setLoading(true)
    setError(null)
    try {
      const result = dateRange
        ? await api.buySellPatternRangeTickers(
            patternName,
            startDate,
            endDate,
            tickerTypes,
            tickers,
            targetPage,
            requestedPageSize,
            orderBy,
          )
        : await api.buySellPatternTickers(patternName, tickerTypes, tickers, targetPage, requestedPageSize, orderBy)
      setRows(result.rows)
      setRowsPatternName(patternName)
      setRowsRange(dateRange ? { start: startDate, end: endDate } : null)
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
    saveReportParams<SavedParams>(paramsId, {
      patternName,
      tickerTypes,
      tickers,
      pageSize: pageSizeInput,
      orderBy,
      ...(dateRange ? { startDate, endDate } : {}),
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
      <h1 className="jobs-page-title">{dateRange ? 'Buy Sell Pattern (Date Range)' : 'Buy Sell Pattern'}</h1>
      {dateRange ? (
        <p className="jobs-page-subtitle">
          Per-ticker trade stats for the chosen pattern, recalculated on each run from the buy-sell-pattern job's
          trades - counting only trades bought and sold within the start and end date. Click View on a row to chart
          its trades.
        </p>
      ) : (
        <p className="jobs-page-subtitle">
          Per-ticker trade stats for the chosen pattern, from the buy-sell-pattern-stats job - run it after the
          buy-sell-pattern job for new patterns to appear. Click View on a row to chart its trades over a date range.
        </p>
      )}

      <div className="report-controls">
        <div className="report-controls-fields">
          <div className="job-field">
            <span className="job-field-label">Pattern</span>
            <select
              value={patternName}
              disabled={!patternNames || patternNames.length === 0}
              onChange={(event) => setPatternName(event.target.value)}
            >
              {!patternNames && <option value="">Loading...</option>}
              {patternNames?.length === 0 && <option value="">No patterns stored yet</option>}
              {patternNames?.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
          {dateRange && (
            <>
              <div className="job-field">
                <span className="job-field-label">Start date</span>
                <input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} />
              </div>
              <div className="job-field">
                <span className="job-field-label">End date</span>
                <input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} />
              </div>
            </>
          )}
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
          <div className="job-field report-page-size-field">
            <span className="job-field-label">Page size</span>
            <input
              type="number"
              min={1}
              max={BUY_SELL_PATTERN_TICKERS_MAX_PAGE_SIZE}
              step={1}
              value={pageSizeInput}
              onChange={(event) => setPageSizeInput(Number(event.target.value))}
              onBlur={() =>
                setPageSizeInput((current) =>
                  Number.isFinite(current)
                    ? Math.min(BUY_SELL_PATTERN_TICKERS_MAX_PAGE_SIZE, Math.max(1, Math.round(current)))
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
            disabled={loading || !patternName || rangeError != null || !Number.isFinite(pageSizeInput) || pageSizeInput < 1}
            onClick={runReport}
          >
            {loading ? 'Running...' : 'Run report'}
          </button>
          <button type="button" className="job-button" onClick={saveParams}>
            {paramsJustSaved ? 'Saved' : 'Save parameters'}
          </button>
        </div>
      </div>

      {rangeError && <p className="jobs-error">{rangeError}</p>}
      {error && <p className="jobs-error">{error}</p>}

      {!loading && rows && (
        <>
          <ReportGrid
            columns={COLUMNS}
            rows={rows}
            rowKey={rowKey}
            formatCell={formatCell}
            emptyMessage={
              rowsRange
                ? 'No tickers have trades under this pattern in this date range.'
                : 'No tickers have trades under this pattern.'
            }
            storageKey={paramsId}
            rowAction={{ label: 'View', onSelect: setPatternRow }}
            exportFilename={
              rowsRange
                ? `buy-sell-pattern-${rowsPatternName}-${rowsRange.start}-${rowsRange.end}`
                : `buy-sell-pattern-${rowsPatternName}`
            }
            exportTitle={
              rowsRange
                ? `Buy Sell Pattern - ${rowsPatternName} (${rowsRange.start} to ${rowsRange.end})`
                : `Buy Sell Pattern - ${rowsPatternName}`
            }
            copyable
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
        <p className="placeholder-note">
          Choose a pattern{dateRange ? ' and date range' : ''}, plus ticker types and tickers (optional), and run the
          report.
        </p>
      )}

      {patternRow && (
        <BuySellPatternModal
          ticker={patternRow.ticker}
          title={`${patternRow.ticker} - Buy Sell Pattern`}
          initialRunName={rowsPatternName}
          initialRange={rowsRange ?? undefined}
          onClose={() => setPatternRow(null)}
        />
      )}
    </div>
  )
}
