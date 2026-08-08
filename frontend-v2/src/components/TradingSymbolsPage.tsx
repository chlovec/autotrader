import { useEffect, useState } from 'react'
import { api, TRADING_SYMBOLS_MAX_PAGE_SIZE, type TickerTypeOption, type TradingSymbolRow } from '../api'
import { ReportGrid, type ReportColumn } from './ReportGrid'
import { SearchableSelect, type SelectOption } from './SearchableSelect'

// Backend caps /ticker-types/search's limit at 50 (see app/main.py's search_ticker_types) -
// ticker_types is a short, mostly-static reference list (db/models.py's TickerType
// docstring), so that's comfortably the whole thing in one page.
const TICKER_TYPE_OPTIONS_LIMIT = 50

const DEFAULT_PAGE_SIZE = 500

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
]

// updated/min_timestamp/fetched_at are naive-UTC (same as JobRun.started_at) - append
// "Z" so Date parses them as UTC instead of local time, same reasoning as JobCard's
// formatTimestamp.
const TIMESTAMP_FIELDS = new Set<keyof TradingSymbolRow>(['updated', 'min_timestamp', 'fetched_at'])

function formatCell(row: TradingSymbolRow, key: keyof TradingSymbolRow): string {
  const value = row[key]
  if (value == null) return '–'
  if (TIMESTAMP_FIELDS.has(key)) return new Date(`${value}Z`).toLocaleString()
  if (typeof value === 'number') return value.toLocaleString(undefined, { maximumFractionDigits: 4 })
  return String(value)
}

function rowKey(row: TradingSymbolRow): string {
  return row.ticker
}

export function TradingSymbolsPage() {
  const [tickerTypeOptions, setTickerTypeOptions] = useState<SelectOption[]>([])
  const [tickerTypes, setTickerTypes] = useState<string[]>([])
  // Draft value bound to the page-size input, distinct from `pageSize` below (the
  // value the currently-displayed page was actually fetched with) - editing this
  // doesn't affect what's on screen, or what Prev/Next page through, until "Run
  // report" is clicked again.
  const [pageSizeInput, setPageSizeInput] = useState(DEFAULT_PAGE_SIZE)
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

  // Fetched once as a static list (rather than SearchableSelect's onSearch, which only
  // queries once the user types) so the dropdown opens showing every ticker type right
  // away - client-side filtered from there as the user types, same component.
  useEffect(() => {
    api
      .searchTickerTypes('', TICKER_TYPE_OPTIONS_LIMIT)
      .then((matches) => setTickerTypeOptions(matches.map((t) => ({ value: t.code, label: tickerTypeLabel(t) }))))
  }, [])

  const fetchPage = async (targetPage: number, requestedPageSize: number) => {
    setLoading(true)
    setError(null)
    try {
      const result = await api.tradingSymbolsReport(tickerTypes, targetPage, requestedPageSize)
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
        <button
          type="button"
          className="job-button job-button-primary"
          disabled={loading || !Number.isFinite(pageSizeInput) || pageSizeInput < 1}
          onClick={runReport}
        >
          {loading ? 'Running...' : 'Run report'}
        </button>
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
    </div>
  )
}
