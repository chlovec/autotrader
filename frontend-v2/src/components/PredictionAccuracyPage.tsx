import { useEffect, useState } from 'react'
import {
  api,
  PREDICTION_ACCURACY_MAX_PAGE_SIZE,
  type PredictionAccuracyOrderField,
  type PredictionAccuracyRow,
  type TickerTypeOption,
} from '../api'
import { loadReportParams, saveReportParams } from '../reportParams'
import { ReportGrid, type ReportColumn } from './ReportGrid'
import { SearchableSelect, type SelectOption } from './SearchableSelect'

const REPORT_PARAMS_ID = 'prediction-accuracy'

type SavedParams = {
  startDate: string
  endDate: string
  tickerTypes: string[]
  tickers: string[]
  pageSize: number
  orderBy: PredictionAccuracyOrderField[]
}

const DEFAULT_PAGE_SIZE = 500

// Fields the backend accepts in `order_by` (see app/main.py's
// PREDICTION_ACCURACY_ORDERABLE_FIELDS).
const ORDER_BY_FIELDS: { key: string; label: string }[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'predicted_date', label: 'Predicted Date' },
  { key: 'price_std', label: 'Price Std' },
  { key: 'markov_error_std', label: 'Markov Error (Std)' },
  { key: 'markov_passed', label: 'Markov Passed' },
  { key: 'mcmc_error_std', label: 'MCMC Error (Std)' },
  { key: 'mcmc_passed', label: 'MCMC Passed' },
  { key: 'lstm_holdout_error_std', label: 'LSTM (Holdout) Error (Std)' },
  { key: 'lstm_holdout_passed', label: 'LSTM (Holdout) Passed' },
  { key: 'lstm_walkforward_error_std', label: 'LSTM (Walk-forward) Error (Std)' },
  { key: 'lstm_walkforward_passed', label: 'LSTM (Walk-forward) Passed' },
]

function tickerTypeLabel(t: TickerTypeOption): string {
  const detail = [t.asset_class, t.description].filter(Boolean).join(': ')
  return detail ? `${t.code} — ${detail}` : t.code
}

// markov_*/mcmc_*/lstm_holdout_*/lstm_walkforward_* quadruples are placed *adjacent*
// per field, same "easy to compare at a glance" reasoning as
// PredictionComparisonPage.COLUMNS.
const COLUMNS: ReportColumn<PredictionAccuracyRow>[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'predicted_date', label: 'Predicted Date' },
  { key: 'actual_exit_price', label: 'Actual Exit Price' },
  { key: 'price_std', label: 'Price Std' },
  { key: 'history_days', label: 'History Days' },
  { key: 'pass_threshold_std', label: 'Pass Threshold (Std)' },
  { key: 'markov_predicted_exit_price', label: 'Markov Predicted Exit Price' },
  { key: 'mcmc_predicted_exit_price', label: 'MCMC Predicted Exit Price' },
  { key: 'lstm_holdout_predicted_exit_price', label: 'LSTM (Holdout) Predicted Exit Price' },
  { key: 'lstm_walkforward_predicted_exit_price', label: 'LSTM (Walk-forward) Predicted Exit Price' },
  { key: 'markov_error', label: 'Markov Error' },
  { key: 'mcmc_error', label: 'MCMC Error' },
  { key: 'lstm_holdout_error', label: 'LSTM (Holdout) Error' },
  { key: 'lstm_walkforward_error', label: 'LSTM (Walk-forward) Error' },
  { key: 'markov_error_std', label: 'Markov Error (Std)' },
  { key: 'mcmc_error_std', label: 'MCMC Error (Std)' },
  { key: 'lstm_holdout_error_std', label: 'LSTM (Holdout) Error (Std)' },
  { key: 'lstm_walkforward_error_std', label: 'LSTM (Walk-forward) Error (Std)' },
  { key: 'markov_passed', label: 'Markov Passed' },
  { key: 'mcmc_passed', label: 'MCMC Passed' },
  { key: 'lstm_holdout_passed', label: 'LSTM (Holdout) Passed' },
  { key: 'lstm_walkforward_passed', label: 'LSTM (Walk-forward) Passed' },
  { key: 'computed_at', label: 'Computed At' },
]

// computed_at is naive-UTC (same as JobRun.started_at) - append "Z" so Date parses it
// as UTC instead of local time, same reasoning as PredictionComparisonPage's
// TIMESTAMP_FIELDS.
const TIMESTAMP_FIELDS = new Set<keyof PredictionAccuracyRow>(['computed_at'])
const BOOLEAN_FIELDS = new Set<keyof PredictionAccuracyRow>([
  'markov_passed',
  'mcmc_passed',
  'lstm_holdout_passed',
  'lstm_walkforward_passed',
])

function formatCell(row: PredictionAccuracyRow, key: keyof PredictionAccuracyRow): string {
  const value = row[key]
  if (value == null) return '–'
  if (TIMESTAMP_FIELDS.has(key)) return new Date(`${value}Z`).toLocaleString()
  if (BOOLEAN_FIELDS.has(key)) return value ? 'Pass' : 'Fail'
  if (typeof value === 'number') return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 })
  return String(value)
}

function rowKey(row: PredictionAccuracyRow): string {
  return `${row.ticker}:${row.predicted_date}`
}

function loadSavedParams(): Partial<SavedParams> | null {
  return loadReportParams<SavedParams>(REPORT_PARAMS_ID)
}

export function PredictionAccuracyPage() {
  // Both optional, unlike PredictionComparisonPage's required predictedDate - blank
  // means unbounded on that side (see app/main.py's prediction_accuracy_report).
  const [startDate, setStartDate] = useState(() => loadSavedParams()?.startDate ?? '')
  const [endDate, setEndDate] = useState(() => loadSavedParams()?.endDate ?? '')
  const [tickerTypeOptions, setTickerTypeOptions] = useState<SelectOption[]>([])
  const [tickerTypes, setTickerTypes] = useState<string[]>(() => loadSavedParams()?.tickerTypes ?? [])
  const [tickers, setTickers] = useState<string[]>(() => loadSavedParams()?.tickers ?? [])
  const [orderBy, setOrderBy] = useState<PredictionAccuracyOrderField[]>(() => loadSavedParams()?.orderBy ?? [])
  const [pageSizeInput, setPageSizeInput] = useState(() => loadSavedParams()?.pageSize ?? DEFAULT_PAGE_SIZE)
  const [page, setPage] = useState(1)
  const [pageInput, setPageInput] = useState(1)
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [total, setTotal] = useState(0)
  const [rows, setRows] = useState<PredictionAccuracyRow[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.searchTickerTypes('', 50).then((matches) => setTickerTypeOptions(matches.map((t) => ({ value: t.code, label: tickerTypeLabel(t) }))))
  }, [])

  const fetchPage = async (targetPage: number, requestedPageSize: number) => {
    setLoading(true)
    setError(null)
    try {
      const result = await api.predictionAccuracyReport(
        startDate,
        endDate,
        tickerTypes,
        tickers,
        targetPage,
        requestedPageSize,
        orderBy,
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
    saveReportParams<SavedParams>(REPORT_PARAMS_ID, { startDate, endDate, tickerTypes, tickers, pageSize: pageSizeInput, orderBy })
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
      <h1 className="jobs-page-title">Prediction Accuracy</h1>
      <p className="jobs-page-subtitle">
        Every (ticker, predicted date) already scored by the Compute prediction accuracy job (see the Jobs page):
        each of the Markov chain, Monte Carlo, LSTM (holdout-trained), and LSTM (walk-forward-trained) sources'
        predicted exit price compared against what actually happened, graded against the ticker's own historical
        volatility. A blank source means that model made no prediction for that (ticker, predicted date).
      </p>

      <div className="report-controls">
        <div className="report-controls-fields">
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
                api.searchTickers(q).then((matches) => matches.map((t) => ({ value: t.ticker, label: t.name ? `${t.ticker} — ${t.name}` : t.ticker })))
              }
              placeholder="Search tickers... (leave blank for all)"
            />
          </div>
        </div>
        <div className="report-controls-fields">
          <div className="job-field report-page-size-field">
            <span className="job-field-label">Page size</span>
            <input
              type="number"
              min={1}
              max={PREDICTION_ACCURACY_MAX_PAGE_SIZE}
              step={1}
              value={pageSizeInput}
              onChange={(event) => setPageSizeInput(Number(event.target.value))}
              onBlur={() =>
                setPageSizeInput((current) =>
                  Number.isFinite(current) ? Math.min(PREDICTION_ACCURACY_MAX_PAGE_SIZE, Math.max(1, Math.round(current))) : DEFAULT_PAGE_SIZE,
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
                        <button type="button" className="order-by-remove" onClick={() => removeOrderField(index)} title="Remove from sort" aria-label="Remove from sort">
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
            emptyMessage="No scored predictions found for this range."
            storageKey="prediction-accuracy"
            exportFilename="prediction-accuracy"
            exportTitle="Prediction Accuracy"
          />
          <div className="report-pager">
            <button type="button" className="job-button" disabled={loading || page <= 1} onClick={() => fetchPage(page - 1, pageSize)}>
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
              of {totalPages} ({total.toLocaleString()} results)
            </span>
            <button type="button" className="job-button" disabled={loading || page >= totalPages} onClick={() => fetchPage(page + 1, pageSize)}>
              Next
            </button>
          </div>
        </>
      )}

      {!rows && !loading && !error && <p className="placeholder-note">Optionally narrow by date range, then run the report.</p>}
    </div>
  )
}
