import { useEffect, useRef, useState } from 'react'
import { api, type TickerTypeOption, type TopMarketMoverRow } from '../api'
import { loadReportParams, saveReportParams } from '../reportParams'
import { ReportGrid, type ReportColumn } from './ReportGrid'
import { SearchableSelect, type SelectOption } from './SearchableSelect'
import { TickerDetailsModal } from './TickerDetailsModal'

const REPORT_PARAMS_ID = 'top-movers'

type SavedParams = {
  tickerTypes: string[]
}

// Backend caps /ticker-types/search's limit at 50 (see app/main.py's search_ticker_types) -
// ticker_types is a short, mostly-static reference list (db/models.py's TickerType
// docstring), so that's comfortably the whole thing in one page.
const TICKER_TYPE_OPTIONS_LIMIT = 50

function tickerTypeLabel(t: TickerTypeOption): string {
  const detail = [t.asset_class, t.description].filter(Boolean).join(': ')
  return detail ? `${t.code} — ${detail}` : t.code
}

const COLUMNS: ReportColumn<TopMarketMoverRow>[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'type', label: 'Type' },
  { key: 'asset_class', label: 'Asset Class' },
  { key: 'average_volume', label: 'Average Volume' },
  { key: 'direction', label: 'Direction' },
  { key: 'rank', label: 'Rank' },
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
const TIMESTAMP_FIELDS = new Set<keyof TopMarketMoverRow>([
  'updated',
  'min_timestamp',
  'fetched_at',
  'prediction_computed_at',
])

function formatCell(row: TopMarketMoverRow, key: keyof TopMarketMoverRow): string {
  const value = row[key]
  if (value == null) return '–'
  if (TIMESTAMP_FIELDS.has(key)) return new Date(`${value}Z`).toLocaleString()
  if (key === 'abs_expected_return_pct') return `${(value as number).toFixed(2)}%`
  if (typeof value === 'number') return value.toLocaleString(undefined, { maximumFractionDigits: 4 })
  return String(value)
}

function rowKey(row: TopMarketMoverRow): string {
  return `${row.ticker}-${row.direction}`
}

export function TopMoversPage() {
  const [tickerTypeOptions, setTickerTypeOptions] = useState<SelectOption[]>([])
  const [tickerTypes, setTickerTypes] = useState<string[]>(
    () => loadReportParams<SavedParams>(REPORT_PARAMS_ID)?.tickerTypes ?? [],
  )
  const [rows, setRows] = useState<TopMarketMoverRow[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [detailsRow, setDetailsRow] = useState<TopMarketMoverRow | null>(null)

  // Fetched once as a static list (rather than SearchableSelect's onSearch, which only
  // queries once the user types) so the dropdown opens showing every ticker type right
  // away - client-side filtered from there as the user types, same component.
  useEffect(() => {
    api
      .searchTickerTypes('', TICKER_TYPE_OPTIONS_LIMIT)
      .then((matches) => setTickerTypeOptions(matches.map((t) => ({ value: t.code, label: tickerTypeLabel(t) }))))
  }, [])

  const runReport = async () => {
    setLoading(true)
    setError(null)
    try {
      setRows(await api.topMoversReport(tickerTypes))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load report')
    } finally {
      setLoading(false)
    }
  }

  // Persists the current draft controls so the next visit to this page restores them -
  // separate from ReportGrid's own "Save view", which only covers sort/filter/freeze/
  // hide/width on the grid itself, not these report-level controls.
  const [paramsJustSaved, setParamsJustSaved] = useState(false)
  const paramsSavedFlashTimeout = useRef<number | null>(null)
  useEffect(() => () => {
    if (paramsSavedFlashTimeout.current) window.clearTimeout(paramsSavedFlashTimeout.current)
  }, [])
  const saveParams = () => {
    saveReportParams<SavedParams>(REPORT_PARAMS_ID, { tickerTypes })
    setParamsJustSaved(true)
    if (paramsSavedFlashTimeout.current) window.clearTimeout(paramsSavedFlashTimeout.current)
    paramsSavedFlashTimeout.current = window.setTimeout(() => setParamsJustSaved(false), 1500)
  }

  return (
    <div className="report-page">
      <h1 className="jobs-page-title">Top Movers</h1>
      <p className="jobs-page-subtitle">
        Today's top-gaining and top-losing tickers. Optionally filter by ticker type before running.
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
        </div>
        <div className="report-controls-actions">
          <button type="button" className="job-button job-button-primary" disabled={loading} onClick={runReport}>
            {loading ? 'Running...' : 'Run report'}
          </button>
          <button type="button" className="job-button" onClick={saveParams}>
            {paramsJustSaved ? 'Saved' : 'Save parameters'}
          </button>
        </div>
      </div>

      {error && <p className="jobs-error">{error}</p>}

      {!loading && rows && (
        <ReportGrid
          columns={COLUMNS}
          rows={rows}
          rowKey={rowKey}
          formatCell={formatCell}
          emptyMessage="No movers found."
          storageKey="top-movers"
          rowContextMenu={[{ label: 'View Details', onSelect: setDetailsRow }]}
        />
      )}

      {!rows && !loading && !error && (
        <p className="placeholder-note">Choose ticker types (optional) and run the report.</p>
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
