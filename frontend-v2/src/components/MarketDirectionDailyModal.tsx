import { useEffect, useState } from 'react'
import { api, type MarketDirectionDailyRow } from '../api'
import { ReportGrid, type ReportColumn } from './ReportGrid'

const COLUMNS: ReportColumn<MarketDirectionDailyRow>[] = [
  { key: 'date', label: 'Date' },
  { key: 'open_price', label: 'Open' },
  { key: 'close_price', label: 'Close' },
  { key: 'pcnt_diff', label: '% Diff' },
  { key: 'market_type', label: 'Market Type' },
]

function formatCell(row: MarketDirectionDailyRow, key: keyof MarketDirectionDailyRow): string {
  const value = row[key]
  if (value == null) return '–'
  if (key === 'pcnt_diff') return `${Number(value).toFixed(2)}%`
  return String(value)
}

function rowKey(row: MarketDirectionDailyRow): string {
  return row.date
}

type MarketDirectionDailyModalProps = {
  ticker: string
  startDate?: string
  endDate?: string
  onClose: () => void
}

// Popped up by the Market Direction report grid's "View daily detail" row menu (see
// MarketDirectionPage's rowContextMenu) - one row per day behind that ticker's pcnt_*
// columns, over the same date range currently applied to the report.
export function MarketDirectionDailyModal({ ticker, startDate, endDate, onClose }: MarketDirectionDailyModalProps) {
  const [rows, setRows] = useState<MarketDirectionDailyRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .marketDirectionDailyReport(ticker, startDate, endDate, [{ field: 'date', dir: 'desc' }])
      .then((result) => {
        if (!cancelled) setRows(result.rows)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load daily detail')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [ticker, startDate, endDate])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal modal-wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="market-direction-daily-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header-row">
          <h2 id="market-direction-daily-title" className="modal-title">
            {ticker} — Daily Detail
          </h2>
          <button type="button" className="icon-button modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        {loading && <p className="placeholder-note">Loading...</p>}
        {error && <p className="jobs-error">{error}</p>}
        {!loading && !error && rows && (
          <ReportGrid
            columns={COLUMNS}
            rows={rows}
            rowKey={rowKey}
            formatCell={formatCell}
            emptyMessage="No daily bars for this ticker in the current date range."
            exportFilename={`${ticker}-daily-market-direction`}
            exportTitle={`${ticker} — Daily Market Direction`}
          />
        )}
      </div>
    </div>
  )
}
