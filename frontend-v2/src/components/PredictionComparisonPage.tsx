import { useEffect, useState } from 'react'
import {
  api,
  PREDICTION_COMPARISON_MAX_PAGE_SIZE,
  type PredictionComparisonOrderField,
  type PredictionComparisonRow,
  type TickerTypeOption,
} from '../api'
import { loadReportParams, saveReportParams } from '../reportParams'
import { ReportGrid, type ReportColumn } from './ReportGrid'
import { SearchableSelect, type SelectOption } from './SearchableSelect'

const REPORT_PARAMS_ID = 'prediction-comparison'

type SavedParams = {
  predictedDate: string
  tickerTypes: string[]
  tickers: string[]
  pageSize: number
  orderBy: PredictionComparisonOrderField[]
}

const DEFAULT_PAGE_SIZE = 500

// Fields the backend accepts in `order_by` (see app/main.py's
// PREDICTION_COMPARISON_ORDERABLE_FIELDS).
const ORDER_BY_FIELDS: { key: string; label: string }[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'markov_predicted_state', label: 'Markov Predicted State' },
  { key: 'markov_state_confidence', label: 'Markov State Confidence' },
  { key: 'markov_expected_return', label: 'Markov Expected Return' },
  { key: 'mcmc_predicted_state', label: 'MCMC Predicted State' },
  { key: 'mcmc_state_confidence', label: 'MCMC State Confidence' },
  { key: 'mcmc_expected_return', label: 'MCMC Expected Return' },
  { key: 'lstm_holdout_predicted_state', label: 'LSTM (Holdout) Predicted State' },
  { key: 'lstm_holdout_state_confidence', label: 'LSTM (Holdout) State Confidence' },
  { key: 'lstm_holdout_expected_return', label: 'LSTM (Holdout) Expected Return' },
  { key: 'lstm_walkforward_predicted_state', label: 'LSTM (Walk-forward) Predicted State' },
  { key: 'lstm_walkforward_state_confidence', label: 'LSTM (Walk-forward) State Confidence' },
  { key: 'lstm_walkforward_expected_return', label: 'LSTM (Walk-forward) Expected Return' },
]

function tickerTypeLabel(t: TickerTypeOption): string {
  const detail = [t.asset_class, t.description].filter(Boolean).join(': ')
  return detail ? `${t.code} — ${detail}` : t.code
}

// One column entry per prediction_comparison_report field (see app/main.py's
// prediction_comparison_report) - markov_*/mcmc_*/lstm_holdout_*/lstm_walkforward_*
// quadruples are placed *adjacent* per field (markov_x, mcmc_x, lstm_holdout_x,
// lstm_walkforward_x, markov_y, ...) so the four models' values for the same field are
// easy to compare at a glance, same reasoning as MarketPredictionsPage.COLUMNS'
// markov_/mcmc_ pairing. lstm_holdout_*/lstm_walkforward_* are two independent
// sources, not one shared "LSTM" column - see PredictionComparisonRow's own comment in
// api.ts for why.
const COLUMNS: ReportColumn<PredictionComparisonRow>[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'predicted_date', label: 'Predicted Date' },
  { key: 'markov_current_state', label: 'Markov Current State' },
  { key: 'mcmc_current_state', label: 'MCMC Current State' },
  { key: 'lstm_holdout_current_state', label: 'LSTM (Holdout) Current State' },
  { key: 'lstm_walkforward_current_state', label: 'LSTM (Walk-forward) Current State' },
  { key: 'markov_predicted_state', label: 'Markov Predicted State' },
  { key: 'mcmc_predicted_state', label: 'MCMC Predicted State' },
  { key: 'lstm_holdout_predicted_state', label: 'LSTM (Holdout) Predicted State' },
  { key: 'lstm_walkforward_predicted_state', label: 'LSTM (Walk-forward) Predicted State' },
  { key: 'markov_state_confidence', label: 'Markov State Confidence' },
  { key: 'mcmc_state_confidence', label: 'MCMC State Confidence' },
  { key: 'lstm_holdout_state_confidence', label: 'LSTM (Holdout) State Confidence' },
  { key: 'lstm_walkforward_state_confidence', label: 'LSTM (Walk-forward) State Confidence' },
  // Full softmax output - LSTM-only, no Markov/MCMC equivalent (see
  // api.ts's PredictionComparisonRow comment).
  { key: 'lstm_holdout_prob_strong_down', label: 'LSTM (Holdout) Prob Strong Down' },
  { key: 'lstm_holdout_prob_down', label: 'LSTM (Holdout) Prob Down' },
  { key: 'lstm_holdout_prob_flat', label: 'LSTM (Holdout) Prob Flat' },
  { key: 'lstm_holdout_prob_up', label: 'LSTM (Holdout) Prob Up' },
  { key: 'lstm_holdout_prob_strong_up', label: 'LSTM (Holdout) Prob Strong Up' },
  { key: 'lstm_walkforward_prob_strong_down', label: 'LSTM (Walk-forward) Prob Strong Down' },
  { key: 'lstm_walkforward_prob_down', label: 'LSTM (Walk-forward) Prob Down' },
  { key: 'lstm_walkforward_prob_flat', label: 'LSTM (Walk-forward) Prob Flat' },
  { key: 'lstm_walkforward_prob_up', label: 'LSTM (Walk-forward) Prob Up' },
  { key: 'lstm_walkforward_prob_strong_up', label: 'LSTM (Walk-forward) Prob Strong Up' },
  { key: 'markov_expected_return', label: 'Markov Expected Return' },
  { key: 'mcmc_expected_return', label: 'MCMC Expected Return' },
  { key: 'lstm_holdout_expected_return', label: 'LSTM (Holdout) Expected Return' },
  { key: 'lstm_walkforward_expected_return', label: 'LSTM (Walk-forward) Expected Return' },
  { key: 'markov_entry_price', label: 'Markov Entry Price' },
  { key: 'mcmc_entry_price', label: 'MCMC Entry Price' },
  { key: 'lstm_holdout_entry_price', label: 'LSTM (Holdout) Entry Price' },
  { key: 'lstm_walkforward_entry_price', label: 'LSTM (Walk-forward) Entry Price' },
  { key: 'markov_exit_price', label: 'Markov Exit Price' },
  { key: 'mcmc_exit_price', label: 'MCMC Exit Price' },
  { key: 'lstm_holdout_exit_price', label: 'LSTM (Holdout) Exit Price' },
  { key: 'lstm_walkforward_exit_price', label: 'LSTM (Walk-forward) Exit Price' },
  { key: 'markov_exit_price_confidence', label: 'Markov Exit Price Confidence' },
  { key: 'mcmc_exit_price_confidence', label: 'MCMC Exit Price Confidence' },
  { key: 'lstm_holdout_exit_price_confidence', label: 'LSTM (Holdout) Exit Price Confidence' },
  { key: 'lstm_walkforward_exit_price_confidence', label: 'LSTM (Walk-forward) Exit Price Confidence' },
  { key: 'mcmc_exit_price_mean', label: 'MCMC Exit Price Mean' },
  { key: 'mcmc_exit_price_std', label: 'MCMC Exit Price Std' },
  { key: 'mcmc_exit_price_p10', label: 'MCMC Exit Price P10' },
  { key: 'mcmc_exit_price_p50', label: 'MCMC Exit Price P50' },
  { key: 'mcmc_exit_price_p90', label: 'MCMC Exit Price P90' },
  { key: 'mcmc_num_simulations', label: 'MCMC Simulated Paths' },
  { key: 'lstm_holdout_model_version_id', label: 'LSTM (Holdout) Model Version' },
  { key: 'lstm_walkforward_model_version_id', label: 'LSTM (Walk-forward) Model Version' },
  { key: 'markov_history_days', label: 'Markov History Days' },
  { key: 'mcmc_history_days', label: 'MCMC History Days' },
  { key: 'lstm_holdout_history_days', label: 'LSTM (Holdout) History Days' },
  { key: 'lstm_walkforward_history_days', label: 'LSTM (Walk-forward) History Days' },
  { key: 'markov_computed_at', label: 'Markov Computed At' },
  { key: 'mcmc_computed_at', label: 'MCMC Computed At' },
  { key: 'lstm_holdout_computed_at', label: 'LSTM (Holdout) Computed At' },
  { key: 'lstm_walkforward_computed_at', label: 'LSTM (Walk-forward) Computed At' },
]

// *_computed_at are naive-UTC (same as JobRun.started_at) - append "Z" so Date parses
// them as UTC instead of local time, same reasoning as MarketPredictionsPage's
// TIMESTAMP_FIELDS.
const TIMESTAMP_FIELDS = new Set<keyof PredictionComparisonRow>([
  'markov_computed_at',
  'mcmc_computed_at',
  'lstm_holdout_computed_at',
  'lstm_walkforward_computed_at',
])

function formatCell(row: PredictionComparisonRow, key: keyof PredictionComparisonRow): string {
  const value = row[key]
  if (value == null) return '–'
  if (TIMESTAMP_FIELDS.has(key)) return new Date(`${value}Z`).toLocaleString()
  if (typeof value === 'number') return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 })
  return String(value)
}

function rowKey(row: PredictionComparisonRow): string {
  return row.ticker
}

function loadSavedParams(): Partial<SavedParams> | null {
  return loadReportParams<SavedParams>(REPORT_PARAMS_ID)
}

export function PredictionComparisonPage() {
  // Required, same "no sensible default date" reasoning as MarketPredictionsPage's
  // predictedDate - runReport is disabled until this is set.
  const [predictedDate, setPredictedDate] = useState(() => loadSavedParams()?.predictedDate ?? '')
  const [tickerTypeOptions, setTickerTypeOptions] = useState<SelectOption[]>([])
  const [tickerTypes, setTickerTypes] = useState<string[]>(() => loadSavedParams()?.tickerTypes ?? [])
  const [tickers, setTickers] = useState<string[]>(() => loadSavedParams()?.tickers ?? [])
  const [orderBy, setOrderBy] = useState<PredictionComparisonOrderField[]>(() => loadSavedParams()?.orderBy ?? [])
  const [pageSizeInput, setPageSizeInput] = useState(() => loadSavedParams()?.pageSize ?? DEFAULT_PAGE_SIZE)
  const [page, setPage] = useState(1)
  const [pageInput, setPageInput] = useState(1)
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [total, setTotal] = useState(0)
  const [rows, setRows] = useState<PredictionComparisonRow[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.searchTickerTypes('', 50).then((matches) => setTickerTypeOptions(matches.map((t) => ({ value: t.code, label: tickerTypeLabel(t) }))))
  }, [])

  const fetchPage = async (targetPage: number, requestedPageSize: number) => {
    if (!predictedDate) return
    setLoading(true)
    setError(null)
    try {
      const result = await api.predictionComparisonReport(
        predictedDate,
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
    saveReportParams<SavedParams>(REPORT_PARAMS_ID, { predictedDate, tickerTypes, tickers, pageSize: pageSizeInput, orderBy })
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
      <h1 className="jobs-page-title">Prediction Comparison</h1>
      <p className="jobs-page-subtitle">
        For a chosen predicted date, every ticker with a Markov chain, Monte Carlo, LSTM (holdout-trained), and/or
        LSTM (walk-forward-trained) prediction for that date, shown side by side - the predict-market-state-family
        jobs (see the Jobs page). The two LSTM flavors are independent models/jobs, not one - a ticker only needs one
        of the four predictions to appear, with the other columns blank if that model didn't produce one for it on
        that date.
      </p>

      <div className="report-controls">
        <div className="report-controls-fields">
          <div className="job-field">
            <span className="job-field-label">Predicted date</span>
            <input type="date" value={predictedDate} onChange={(event) => setPredictedDate(event.target.value)} />
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
              max={PREDICTION_COMPARISON_MAX_PAGE_SIZE}
              step={1}
              value={pageSizeInput}
              onChange={(event) => setPageSizeInput(Number(event.target.value))}
              onBlur={() =>
                setPageSizeInput((current) =>
                  Number.isFinite(current) ? Math.min(PREDICTION_COMPARISON_MAX_PAGE_SIZE, Math.max(1, Math.round(current))) : DEFAULT_PAGE_SIZE,
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
            disabled={loading || !predictedDate || !Number.isFinite(pageSizeInput) || pageSizeInput < 1}
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
            emptyMessage="No predictions found for this date."
            storageKey="prediction-comparison"
            exportFilename="prediction-comparison"
            exportTitle="Prediction Comparison"
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
              of {totalPages} ({total.toLocaleString()} tickers)
            </span>
            <button type="button" className="job-button" disabled={loading || page >= totalPages} onClick={() => fetchPage(page + 1, pageSize)}>
              Next
            </button>
          </div>
        </>
      )}

      {!rows && !loading && !error && <p className="placeholder-note">Choose a predicted date (required) and run the report.</p>}
    </div>
  )
}
