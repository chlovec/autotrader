import { useEffect, useRef, useState } from 'react'
import {
  api,
  MARKET_PREDICTIONS_MAX_PAGE_SIZE,
  type MarketPredictionOrderField,
  type MarketPredictionRow,
  type TickerTypeOption,
} from '../api'
import { NUMERIC_FILTER_OPS, type NumericFilterOp } from '../numericFilter'
import { loadReportParams, saveReportParams } from '../reportParams'
import { ReportGrid, type ReportColumn } from './ReportGrid'
import { SearchableSelect, type SelectOption } from './SearchableSelect'
import { TickerDetailsModal } from './TickerDetailsModal'

const REPORT_PARAMS_ID = 'market-predictions'

// One entry per numeric filter this page has (see useNumericFilterField below) -
// each stored as an independent op/value pair, same shape every numeric report
// filter in this app persists as (see e.g. TradingSymbolsPage's SavedParams).
type SavedParams = {
  predictedDate: string
  tickerTypes: string[]
  tickers: string[]
  pageSize: number
  orderBy: MarketPredictionOrderField[]
  markovExitPriceConfidenceOp: NumericFilterOp | ''
  markovExitPriceConfidenceValue: string
  mcmcExitPriceConfidenceOp: NumericFilterOp | ''
  mcmcExitPriceConfidenceValue: string
  averageVolumeOp: NumericFilterOp | ''
  averageVolumeValue: string
  marketCapOp: NumericFilterOp | ''
  marketCapValue: string
  markovHistoryDaysOp: NumericFilterOp | ''
  markovHistoryDaysValue: string
  mcmcHistoryDaysOp: NumericFilterOp | ''
  mcmcHistoryDaysValue: string
  markovStateConfidenceOp: NumericFilterOp | ''
  markovStateConfidenceValue: string
  mcmcStateConfidenceOp: NumericFilterOp | ''
  mcmcStateConfidenceValue: string
  markovExpectedReturnOp: NumericFilterOp | ''
  markovExpectedReturnValue: string
  mcmcExpectedReturnOp: NumericFilterOp | ''
  mcmcExpectedReturnValue: string
  mcmcP10ReturnPctOp: NumericFilterOp | ''
  mcmcP10ReturnPctValue: string
  validationScoreOp: NumericFilterOp | ''
  validationScoreValue: string
  survivorScoreOp: NumericFilterOp | ''
  survivorScoreValue: string
  markovPredictedStates: string[]
  mcmcPredictedStates: string[]
  requireConsensus: boolean
}

// Direction states a prediction can take (see jobs/predict_market_state.py's
// STATE_LABELS) - a fixed, small set, so a static option list rather than
// SearchableSelect's async onSearch (used for tickers, whose universe is too large
// to list up front).
const PREDICTED_STATE_OPTIONS: SelectOption[] = [
  { value: 'strong_down', label: 'Strong Down' },
  { value: 'down', label: 'Down' },
  { value: 'flat', label: 'Flat' },
  { value: 'up', label: 'Up' },
  { value: 'strong_up', label: 'Strong Up' },
]

// One op/value pair of local state plus the NumericFilter it resolves to (undefined
// until both halves are a valid number), same "both required together, otherwise no
// filter" rule app/main.py's numeric report filters all enforce. Pulled into a hook
// since this page has 11 independent numeric filters - inlining each one's op/value
// useState pair plus the op/value-to-filter derivation (as the two exit-price-
// confidence filters used to, before this page grew the rest) would repeat the same
// ~10 lines 11 times over.
function useNumericFilterField(initialOp: NumericFilterOp | '', initialValue: string) {
  const [op, setOp] = useState<NumericFilterOp | ''>(initialOp)
  const [value, setValue] = useState(initialValue)
  const filter = op && value.trim() !== '' && Number.isFinite(Number(value)) ? { op, value: Number(value) } : undefined
  return { op, setOp, value, setValue, filter }
}

// Renders one useNumericFilterField's op-select + value-input pair - shared by every
// numeric filter row below instead of repeating the same ~15-line <select>/<input>
// JSX block 11 times over.
function NumericFilterField({ label, field }: { label: string; field: ReturnType<typeof useNumericFilterField> }) {
  return (
    <div className="job-field report-numeric-filter-field">
      <span className="job-field-label">{label}</span>
      <div className="report-numeric-filter-inputs">
        <select value={field.op} onChange={(event) => field.setOp(event.target.value as NumericFilterOp | '')}>
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
          value={field.value}
          onChange={(event) => field.setValue(event.target.value)}
        />
      </div>
    </div>
  )
}

// Backend caps /ticker-types/search's limit at 50 (see app/main.py's search_ticker_types) -
// same reasoning as TradingSymbolsPage.
const TICKER_TYPE_OPTIONS_LIMIT = 50

const DEFAULT_PAGE_SIZE = 500

// Fields the backend accepts in `order_by` (see MARKET_PREDICTIONS_ORDERABLE_FIELDS in
// app/main.py).
const ORDER_BY_FIELDS: { key: string; label: string }[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'average_volume', label: 'Average Volume' },
  { key: 'market_cap', label: 'Market Cap' },
  { key: 'validation_score', label: 'Validation Score' },
  { key: 'survivor_score', label: 'Survivor Score' },
  { key: 'markov_state_confidence', label: 'Markov State Confidence' },
  { key: 'markov_expected_return', label: 'Markov Expected Return' },
  { key: 'markov_exit_price_confidence', label: 'Markov Exit Price Confidence' },
  { key: 'markov_history_days', label: 'Markov History Days' },
  { key: 'mcmc_state_confidence', label: 'MCMC State Confidence' },
  { key: 'mcmc_expected_return', label: 'MCMC Expected Return' },
  { key: 'mcmc_exit_price_confidence', label: 'MCMC Exit Price Confidence' },
  { key: 'mcmc_history_days', label: 'MCMC History Days' },
]

function tickerTypeLabel(t: TickerTypeOption): string {
  const detail = [t.asset_class, t.description].filter(Boolean).join(': ')
  return detail ? `${t.code} — ${detail}` : t.code
}

// One column entry per market_predictions_report field (see app/main.py's
// _market_prediction_row_to_dict) - markov_*/mcmc_* pair up the predict-market-state
// job's two phases, the deterministic Markov chain prediction and the Monte Carlo
// simulation that runs immediately after it, placed *adjacent* to each other field-by-field (markov_x, mcmc_x, markov_y, mcmc_y,
// ...) rather than grouped into two separate blocks, so the two predictions' values
// for the same field are easy to compare at a glance without scrolling. MCMC's
// distribution-only fields (exit_price_mean/std/p10/p50/p90, num_simulations) have no
// Markov counterpart to pair with, so they sit right after the nearest shared field
// they elaborate on (exit_price_confidence, history_days) instead. Same
// "no metaprogrammed columns" reasoning as TradingSymbolsPage's COLUMNS. Unlike that
// report, there's a single predicted_date column (not markov_predicted_date/
// mcmc_predicted_date) - every row is for the one date the report was run for (see
// the Predicted Date field below), never null.
const COLUMNS: ReportColumn<MarketPredictionRow>[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'average_volume', label: 'Average Volume' },
  { key: 'market_cap', label: 'Market Cap' },
  { key: 'validation_score', label: 'Validation Score' },
  { key: 'survivor_score', label: 'Survivor Score' },
  { key: 'predicted_date', label: 'Predicted Date' },
  { key: 'markov_current_state', label: 'Markov Current State' },
  { key: 'mcmc_current_state', label: 'MCMC Current State' },
  { key: 'markov_predicted_state', label: 'Markov Predicted State' },
  { key: 'mcmc_predicted_state', label: 'MCMC Predicted State' },
  { key: 'markov_state_confidence', label: 'Markov State Confidence' },
  { key: 'mcmc_state_confidence', label: 'MCMC State Confidence' },
  { key: 'markov_expected_return', label: 'Markov Expected Return' },
  { key: 'mcmc_expected_return', label: 'MCMC Expected Return' },
  { key: 'markov_expected_return_pct', label: 'Markov Expected Return %' },
  { key: 'mcmc_expected_return_pct', label: 'MCMC Expected Return %' },
  { key: 'markov_entry_price', label: 'Markov Entry Price' },
  { key: 'mcmc_entry_price', label: 'MCMC Entry Price' },
  { key: 'markov_exit_price', label: 'Markov Exit Price' },
  { key: 'mcmc_exit_price', label: 'MCMC Exit Price' },
  { key: 'markov_exit_price_confidence', label: 'Markov Exit Price Confidence' },
  { key: 'mcmc_exit_price_confidence', label: 'MCMC Exit Price Confidence' },
  { key: 'mcmc_exit_price_mean', label: 'MCMC Exit Price Mean' },
  { key: 'mcmc_exit_price_std', label: 'MCMC Exit Price Std' },
  { key: 'mcmc_exit_price_p10', label: 'MCMC Exit Price P10' },
  { key: 'mcmc_exit_price_p50', label: 'MCMC Exit Price P50' },
  { key: 'mcmc_exit_price_p90', label: 'MCMC Exit Price P90' },
  { key: 'markov_entry_time', label: 'Markov Entry Time' },
  { key: 'mcmc_entry_time', label: 'MCMC Entry Time' },
  { key: 'markov_exit_time', label: 'Markov Exit Time' },
  { key: 'mcmc_exit_time', label: 'MCMC Exit Time' },
  { key: 'mcmc_num_simulations', label: 'MCMC Simulated Paths' },
  { key: 'markov_history_days', label: 'Markov History Days' },
  { key: 'mcmc_history_days', label: 'MCMC History Days' },
  { key: 'markov_computed_at', label: 'Markov Computed At' },
  { key: 'mcmc_computed_at', label: 'MCMC Computed At' },
]

// markov_computed_at/mcmc_computed_at are naive-UTC (same as JobRun.started_at) -
// append "Z" so Date parses them as UTC instead of local time, same reasoning as
// JobCard's formatTimestamp. predicted_date is a plain date (no time component), so
// it's left out of this set and rendered as-is by formatCell, same as
// TradingSymbolsPage's predicted_date.
const TIMESTAMP_FIELDS = new Set<keyof MarketPredictionRow>(['markov_computed_at', 'mcmc_computed_at'])

// markov_expected_return_pct/mcmc_expected_return_pct are already *100 (see api.ts's
// withExpectedReturnPct) - this just appends the "%" formatCell's generic number
// branch below doesn't know to add.
const PCT_FIELDS = new Set<keyof MarketPredictionRow>(['markov_expected_return_pct', 'mcmc_expected_return_pct'])

function formatCell(row: MarketPredictionRow, key: keyof MarketPredictionRow): string {
  const value = row[key]
  if (value == null) return '–'
  if (TIMESTAMP_FIELDS.has(key)) return new Date(`${value}Z`).toLocaleString()
  if (typeof value === 'number') {
    const formatted = value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    return PCT_FIELDS.has(key) ? `${formatted}%` : formatted
  }
  return String(value)
}

function rowKey(row: MarketPredictionRow): string {
  return row.ticker
}

// Computed once on module load (not per-mount) - same reasoning as TradingSymbolsPage's
// loadSavedParams.
function loadSavedParams(): Partial<SavedParams> | null {
  return loadReportParams<SavedParams>(REPORT_PARAMS_ID)
}

export function MarketPredictionsPage() {
  // Required (see app/main.py's market_predictions_report) - unlike every other
  // report's filters, there's no "leave blank for all" here, since the report has no
  // sensible default date to fall back to. runReport is disabled until this is set.
  const [predictedDate, setPredictedDate] = useState(() => loadSavedParams()?.predictedDate ?? '')
  const [tickerTypeOptions, setTickerTypeOptions] = useState<SelectOption[]>([])
  const [tickerTypes, setTickerTypes] = useState<string[]>(() => loadSavedParams()?.tickerTypes ?? [])
  const [tickers, setTickers] = useState<string[]>(() => loadSavedParams()?.tickers ?? [])
  const [orderBy, setOrderBy] = useState<MarketPredictionOrderField[]>(() => loadSavedParams()?.orderBy ?? [])
  // Filters which markov_/mcmc_ prediction rows can qualify a ticker for inclusion -
  // markov_*/mcmc_* filters below each apply directly to their own column as an
  // independent global AND condition (see app/main.py's market_predictions_report
  // docstring), same as averageVolume/marketCap/validationScore/survivorScore and
  // requireConsensus. A ticker only needs a prediction from either source to appear
  // at all; a set filter then excludes tickers that don't have a satisfying value on
  // that side, rather than letting them through via the other side.
  const markovExitPriceConfidence = useNumericFilterField(
    loadSavedParams()?.markovExitPriceConfidenceOp ?? '',
    loadSavedParams()?.markovExitPriceConfidenceValue ?? '',
  )
  const mcmcExitPriceConfidence = useNumericFilterField(
    loadSavedParams()?.mcmcExitPriceConfidenceOp ?? '',
    loadSavedParams()?.mcmcExitPriceConfidenceValue ?? '',
  )
  const averageVolume = useNumericFilterField(loadSavedParams()?.averageVolumeOp ?? '', loadSavedParams()?.averageVolumeValue ?? '')
  const marketCap = useNumericFilterField(loadSavedParams()?.marketCapOp ?? '', loadSavedParams()?.marketCapValue ?? '')
  const markovHistoryDays = useNumericFilterField(
    loadSavedParams()?.markovHistoryDaysOp ?? '',
    loadSavedParams()?.markovHistoryDaysValue ?? '',
  )
  const mcmcHistoryDays = useNumericFilterField(
    loadSavedParams()?.mcmcHistoryDaysOp ?? '',
    loadSavedParams()?.mcmcHistoryDaysValue ?? '',
  )
  const markovStateConfidence = useNumericFilterField(
    loadSavedParams()?.markovStateConfidenceOp ?? '',
    loadSavedParams()?.markovStateConfidenceValue ?? '',
  )
  const mcmcStateConfidence = useNumericFilterField(
    loadSavedParams()?.mcmcStateConfidenceOp ?? '',
    loadSavedParams()?.mcmcStateConfidenceValue ?? '',
  )
  const markovExpectedReturn = useNumericFilterField(
    loadSavedParams()?.markovExpectedReturnOp ?? '',
    loadSavedParams()?.markovExpectedReturnValue ?? '',
  )
  const mcmcExpectedReturn = useNumericFilterField(
    loadSavedParams()?.mcmcExpectedReturnOp ?? '',
    loadSavedParams()?.mcmcExpectedReturnValue ?? '',
  )
  const mcmcP10ReturnPct = useNumericFilterField(
    loadSavedParams()?.mcmcP10ReturnPctOp ?? '',
    loadSavedParams()?.mcmcP10ReturnPctValue ?? '',
  )
  const validationScore = useNumericFilterField(loadSavedParams()?.validationScoreOp ?? '', loadSavedParams()?.validationScoreValue ?? '')
  const survivorScore = useNumericFilterField(loadSavedParams()?.survivorScoreOp ?? '', loadSavedParams()?.survivorScoreValue ?? '')
  const [markovPredictedStates, setMarkovPredictedStates] = useState<string[]>(() => loadSavedParams()?.markovPredictedStates ?? [])
  const [mcmcPredictedStates, setMcmcPredictedStates] = useState<string[]>(() => loadSavedParams()?.mcmcPredictedStates ?? [])
  const [requireConsensus, setRequireConsensus] = useState(() => loadSavedParams()?.requireConsensus ?? false)
  const [pageSizeInput, setPageSizeInput] = useState(() => loadSavedParams()?.pageSize ?? DEFAULT_PAGE_SIZE)
  const [page, setPage] = useState(1)
  const [pageInput, setPageInput] = useState(1)
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [total, setTotal] = useState(0)
  const [rows, setRows] = useState<MarketPredictionRow[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [detailsRow, setDetailsRow] = useState<MarketPredictionRow | null>(null)

  useEffect(() => {
    api
      .searchTickerTypes('', TICKER_TYPE_OPTIONS_LIMIT)
      .then((matches) => setTickerTypeOptions(matches.map((t) => ({ value: t.code, label: tickerTypeLabel(t) }))))
  }, [])

  const fetchPage = async (targetPage: number, requestedPageSize: number) => {
    if (!predictedDate) return
    setLoading(true)
    setError(null)
    try {
      const result = await api.marketPredictionsReport({
        predictedDate,
        tickerTypes,
        tickers,
        page: targetPage,
        pageSize: requestedPageSize,
        orderBy,
        markovExitPriceConfidenceFilter: markovExitPriceConfidence.filter,
        mcmcExitPriceConfidenceFilter: mcmcExitPriceConfidence.filter,
        averageVolumeFilter: averageVolume.filter,
        marketCapFilter: marketCap.filter,
        markovHistoryDaysFilter: markovHistoryDays.filter,
        mcmcHistoryDaysFilter: mcmcHistoryDays.filter,
        markovStateConfidenceFilter: markovStateConfidence.filter,
        mcmcStateConfidenceFilter: mcmcStateConfidence.filter,
        markovExpectedReturnFilter: markovExpectedReturn.filter,
        mcmcExpectedReturnFilter: mcmcExpectedReturn.filter,
        markovPredictedStates,
        mcmcPredictedStates,
        requireConsensus,
        mcmcP10ReturnPctFilter: mcmcP10ReturnPct.filter,
        validationScoreFilter: validationScore.filter,
        survivorScoreFilter: survivorScore.filter,
      })
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
      predictedDate,
      tickerTypes,
      tickers,
      pageSize: pageSizeInput,
      orderBy,
      markovExitPriceConfidenceOp: markovExitPriceConfidence.op,
      markovExitPriceConfidenceValue: markovExitPriceConfidence.value,
      mcmcExitPriceConfidenceOp: mcmcExitPriceConfidence.op,
      mcmcExitPriceConfidenceValue: mcmcExitPriceConfidence.value,
      averageVolumeOp: averageVolume.op,
      averageVolumeValue: averageVolume.value,
      marketCapOp: marketCap.op,
      marketCapValue: marketCap.value,
      markovHistoryDaysOp: markovHistoryDays.op,
      markovHistoryDaysValue: markovHistoryDays.value,
      mcmcHistoryDaysOp: mcmcHistoryDays.op,
      mcmcHistoryDaysValue: mcmcHistoryDays.value,
      markovStateConfidenceOp: markovStateConfidence.op,
      markovStateConfidenceValue: markovStateConfidence.value,
      mcmcStateConfidenceOp: mcmcStateConfidence.op,
      mcmcStateConfidenceValue: mcmcStateConfidence.value,
      markovExpectedReturnOp: markovExpectedReturn.op,
      markovExpectedReturnValue: markovExpectedReturn.value,
      mcmcExpectedReturnOp: mcmcExpectedReturn.op,
      mcmcExpectedReturnValue: mcmcExpectedReturn.value,
      mcmcP10ReturnPctOp: mcmcP10ReturnPct.op,
      mcmcP10ReturnPctValue: mcmcP10ReturnPct.value,
      validationScoreOp: validationScore.op,
      validationScoreValue: validationScore.value,
      survivorScoreOp: survivorScore.op,
      survivorScoreValue: survivorScore.value,
      markovPredictedStates,
      mcmcPredictedStates,
      requireConsensus,
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
      <h1 className="jobs-page-title">Market Predictions</h1>
      <p className="jobs-page-subtitle">
        For a chosen predicted date, every ticker with a Markov chain and/or a Monte Carlo prediction for that date -
        the two phases of the predict-market-state job (see the Jobs page) - shown side by side. A ticker only needs
        one of the two predictions to appear - the other side's columns are blank if that phase didn't produce one
        for it on that date.
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
                api
                  .searchTickers(q)
                  .then((matches) => matches.map((t) => ({ value: t.ticker, label: t.name ? `${t.ticker} — ${t.name}` : t.ticker })))
              }
              placeholder="Search tickers... (leave blank for all)"
            />
          </div>
        </div>
        <div className="report-controls-fields">
          <NumericFilterField label="Markov exit price confidence" field={markovExitPriceConfidence} />
          <NumericFilterField label="MCMC exit price confidence" field={mcmcExitPriceConfidence} />
        </div>
        <div className="report-controls-fields">
          <NumericFilterField label="Average volume" field={averageVolume} />
          <NumericFilterField label="Market cap" field={marketCap} />
        </div>
        <div className="report-controls-fields">
          <NumericFilterField label="Markov history days" field={markovHistoryDays} />
          <NumericFilterField label="MCMC history days" field={mcmcHistoryDays} />
        </div>
        <div className="report-controls-fields">
          <NumericFilterField label="Markov state confidence" field={markovStateConfidence} />
          <NumericFilterField label="MCMC state confidence" field={mcmcStateConfidence} />
        </div>
        <div className="report-controls-fields">
          <NumericFilterField label="Markov expected return" field={markovExpectedReturn} />
          <NumericFilterField label="MCMC expected return" field={mcmcExpectedReturn} />
        </div>
        <div className="report-controls-fields">
          <div className="job-field report-ticker-type-field">
            <span className="job-field-label">Markov predicted state</span>
            <SearchableSelect
              multiple
              selected={markovPredictedStates}
              onChange={setMarkovPredictedStates}
              options={PREDICTED_STATE_OPTIONS}
              placeholder="Any state"
            />
          </div>
          <div className="job-field report-ticker-type-field">
            <span className="job-field-label">MCMC predicted state</span>
            <SearchableSelect
              multiple
              selected={mcmcPredictedStates}
              onChange={setMcmcPredictedStates}
              options={PREDICTED_STATE_OPTIONS}
              placeholder="Any state"
            />
          </div>
        </div>
        <div className="report-controls-fields">
          <div className="job-field report-checkbox-field">
            <span className="job-field-label">Consensus</span>
            <label className="job-run-type-option">
              <input type="checkbox" checked={requireConsensus} onChange={(event) => setRequireConsensus(event.target.checked)} />
              Require Markov and MCMC to agree on predicted state
            </label>
          </div>
          <NumericFilterField label="MCMC P10 return %" field={mcmcP10ReturnPct} />
        </div>
        <div className="report-controls-fields">
          <NumericFilterField label="Validation score" field={validationScore} />
          <NumericFilterField label="Survivor score" field={survivorScore} />
        </div>
        <div className="report-controls-fields">
          <div className="job-field report-page-size-field">
            <span className="job-field-label">Page size</span>
            <input
              type="number"
              min={1}
              max={MARKET_PREDICTIONS_MAX_PAGE_SIZE}
              step={1}
              value={pageSizeInput}
              onChange={(event) => setPageSizeInput(Number(event.target.value))}
              onBlur={() =>
                setPageSizeInput((current) =>
                  Number.isFinite(current) ? Math.min(MARKET_PREDICTIONS_MAX_PAGE_SIZE, Math.max(1, Math.round(current))) : DEFAULT_PAGE_SIZE,
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
            storageKey="market-predictions"
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
        <p className="placeholder-note">Choose a predicted date (required) and run the report.</p>
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
