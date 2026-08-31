import { useEffect, useState } from 'react'
import {
  api,
  MARKET_DIRECTION_MAX_PAGE_SIZE,
  type MarketDirectionOrderField,
  type MarketDirectionRow,
  type TickerTypeOption,
} from '../api'
import { NUMERIC_FILTER_OPS, type NumericFilterOp } from '../numericFilter'
import { loadReportParams, saveReportParams } from '../reportParams'
import { MarketDirectionDailyModal } from './MarketDirectionDailyModal'
import { ReportGrid, type ReportColumn } from './ReportGrid'
import { SearchableSelect, type SelectOption } from './SearchableSelect'

const REPORT_PARAMS_ID = 'market-direction'

type SavedParams = {
  startDate: string
  endDate: string
  tickerTypes: string[]
  tickers: string[]
  pageSize: number
  orderBy: MarketDirectionOrderField[]
  marketCapOp: NumericFilterOp | ''
  marketCapValue: string
}

// Same reasoning as TradingSymbolsPage's TICKER_TYPE_OPTIONS_LIMIT.
const TICKER_TYPE_OPTIONS_LIMIT = 50

const DEFAULT_PAGE_SIZE = 500

// Fields the backend accepts in `order_by` (see MARKET_DIRECTION_ORDERABLE_FIELDS in
// app/main.py) - every column COLUMNS below shows.
const ORDER_BY_FIELDS: { key: string; label: string }[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'type', label: 'Type' },
  { key: 'market', label: 'Market' },
  { key: 'latest_price', label: 'Latest Price' },
  { key: 'market_cap', label: 'Market Cap' },
  { key: 'total_records', label: 'Total Records' },
  { key: 'pcnt_strong_down', label: '% Strong Down' },
  { key: 'pcnt_down', label: '% Down' },
  { key: 'pcnt_neutral', label: '% Neutral' },
  { key: 'pcnt_up', label: '% Up' },
  { key: 'pcnt_strong_up', label: '% Strong Up' },
]

function tickerTypeLabel(t: TickerTypeOption): string {
  const detail = [t.asset_class, t.description].filter(Boolean).join(': ')
  return detail ? `${t.code} — ${detail}` : t.code
}

const COLUMNS: ReportColumn<MarketDirectionRow>[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'type', label: 'Type' },
  { key: 'market', label: 'Market' },
  { key: 'latest_price', label: 'Latest Price' },
  { key: 'market_cap', label: 'Market Cap' },
  { key: 'total_records', label: 'Total Records' },
  { key: 'pcnt_strong_down', label: '% Strong Down' },
  { key: 'pcnt_down', label: '% Down' },
  { key: 'pcnt_neutral', label: '% Neutral' },
  { key: 'pcnt_up', label: '% Up' },
  { key: 'pcnt_strong_up', label: '% Strong Up' },
]

const PERCENT_FIELDS = new Set<keyof MarketDirectionRow>([
  'pcnt_strong_down',
  'pcnt_down',
  'pcnt_neutral',
  'pcnt_up',
  'pcnt_strong_up',
])

function formatCell(row: MarketDirectionRow, key: keyof MarketDirectionRow): string {
  const value = row[key]
  if (value == null) return '–'
  if (PERCENT_FIELDS.has(key)) return `${Number(value).toFixed(2)}%`
  if (key === 'market_cap') return Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 })
  return String(value)
}

function rowKey(row: MarketDirectionRow): string {
  return row.ticker
}

// Computed once on module load, not per-mount - same reasoning as
// TradingSymbolsPage's loadSavedParams.
function loadSavedParams(): Partial<SavedParams> | null {
  return loadReportParams<SavedParams>(REPORT_PARAMS_ID)
}

export function MarketDirectionPage() {
  const [tickerTypeOptions, setTickerTypeOptions] = useState<SelectOption[]>([])
  const [startDate, setStartDate] = useState(() => loadSavedParams()?.startDate ?? '')
  const [endDate, setEndDate] = useState(() => loadSavedParams()?.endDate ?? '')
  const [tickerTypes, setTickerTypes] = useState<string[]>(() => loadSavedParams()?.tickerTypes ?? [])
  const [tickers, setTickers] = useState<string[]>(() => loadSavedParams()?.tickers ?? [])
  const [orderBy, setOrderBy] = useState<MarketDirectionOrderField[]>(() => loadSavedParams()?.orderBy ?? [])
  // A real backend filter (see app/main.py's market_direction_report), same
  // shape/reasoning as MarketPredictionsPerformancePage's marketCapOp/marketCapValue.
  const [marketCapOp, setMarketCapOp] = useState<NumericFilterOp | ''>(() => loadSavedParams()?.marketCapOp ?? '')
  const [marketCapValue, setMarketCapValue] = useState(() => loadSavedParams()?.marketCapValue ?? '')
  const [pageSizeInput, setPageSizeInput] = useState(() => loadSavedParams()?.pageSize ?? DEFAULT_PAGE_SIZE)
  const [page, setPage] = useState(1)
  const [pageInput, setPageInput] = useState(1)
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [total, setTotal] = useState(0)
  const [rows, setRows] = useState<MarketDirectionRow[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dailyDetailTicker, setDailyDetailTicker] = useState<string | null>(null)

  useEffect(() => {
    api
      .searchTickerTypes('', TICKER_TYPE_OPTIONS_LIMIT)
      .then((matches) => setTickerTypeOptions(matches.map((t) => ({ value: t.code, label: tickerTypeLabel(t) }))))
  }, [])

  // undefined (not sent to the backend at all) until both an operator is chosen and a
  // parseable value typed - same reasoning as MarketPredictionsPerformancePage's
  // marketCapFilter.
  const marketCapFilter =
    marketCapOp && marketCapValue.trim() !== '' && Number.isFinite(Number(marketCapValue))
      ? { op: marketCapOp, value: Number(marketCapValue) }
      : undefined

  const fetchPage = async (targetPage: number, requestedPageSize: number) => {
    setLoading(true)
    setError(null)
    try {
      const result = await api.marketDirectionReport(
        startDate || undefined,
        endDate || undefined,
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
    })
    setParamsJustSaved(true)
    window.setTimeout(() => setParamsJustSaved(false), 1500)
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
      <h1 className="jobs-page-title">Market Direction</h1>
      <p className="jobs-page-subtitle">
        For each ticker, the share of its daily bars in the chosen date range that closed strong down, down, neutral,
        up, or strong up (from tickers_daily_market_direction's open-to-close move). Leave both dates blank to
        default to today. Optionally filter by ticker type or specific tickers before running.
      </p>

      <div className="report-controls">
        <div className="report-controls-fields market-direction-fields">
          <div className="job-field">
            <span className="job-field-label">Start date</span>
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
              max={MARKET_DIRECTION_MAX_PAGE_SIZE}
              step={1}
              value={pageSizeInput}
              onChange={(event) => setPageSizeInput(Number(event.target.value))}
              onBlur={() =>
                setPageSizeInput((current) =>
                  Number.isFinite(current) ? Math.min(MARKET_DIRECTION_MAX_PAGE_SIZE, Math.max(1, Math.round(current))) : DEFAULT_PAGE_SIZE,
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
            emptyMessage="No tickers with bars in this range."
            storageKey={REPORT_PARAMS_ID}
            exportFilename="market-direction"
            exportTitle="Market Direction"
            rowContextMenu={[{ label: 'View daily detail', onSelect: (row) => setDailyDetailTicker(row.ticker) }]}
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
        <p className="placeholder-note">Choose a date range (optional) and run the report.</p>
      )}

      {dailyDetailTicker && (
        <MarketDirectionDailyModal
          ticker={dailyDetailTicker}
          startDate={startDate || undefined}
          endDate={endDate || undefined}
          onClose={() => setDailyDetailTicker(null)}
        />
      )}
    </div>
  )
}
