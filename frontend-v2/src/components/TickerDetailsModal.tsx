import { useEffect, useRef, useState } from 'react'
import { BacktestChart } from './BacktestChart'
import { ColumnsMenu, type ReportColumn } from './ReportGrid'

const HIDDEN_FIELDS_STORAGE_KEY = 'view-details-hidden-fields'

// Global (not per-report, not per-ticker) - a field hidden while viewing one report's
// "View Details" popup stays hidden for every other report's popup too, per the
// feature's own requirement. Any report's field keys can end up in here; a report
// whose columns don't include a given key just never matches it in visibleColumns
// below, so a key hidden while looking at a Trading Symbols ticker (e.g. `direction`,
// which only Top Movers has) is harmless dead weight rather than an error.
function loadHiddenFields(): Set<string> {
  try {
    const raw = window.localStorage.getItem(HIDDEN_FIELDS_STORAGE_KEY)
    if (!raw) return new Set()
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? new Set(parsed.filter((value): value is string => typeof value === 'string')) : new Set()
  } catch {
    return new Set()
  }
}

function saveHiddenFields(keys: Set<string>): void {
  try {
    window.localStorage.setItem(HIDDEN_FIELDS_STORAGE_KEY, JSON.stringify(Array.from(keys)))
  } catch {
    // Storage full/unavailable - same reasoning as ReportGrid's saveView, a nice-to-have.
  }
}

function clearHiddenFields(): void {
  try {
    window.localStorage.removeItem(HIDDEN_FIELDS_STORAGE_KEY)
  } catch {
    // ignore, same as saveHiddenFields above
  }
}

type TickerDetailsModalProps<T> = {
  row: T
  title: string
  columns: ReportColumn<T>[]
  formatCell: (row: T, key: Extract<keyof T, string>) => string
  onClose: () => void
}

// Popped up by a row's right-click "View Details" menu (see ReportGrid's
// rowContextMenu prop) - the same row data the grid already has, just laid out as a
// multi-column field grid instead of one wide table row. Which fields are hidden is
// persisted globally (loadHiddenFields/saveHiddenFields above), independent of
// ReportGrid's own per-report "Save view" (which only covers the grid itself).
//
// T is constrained to { ticker: string } (rather than left fully generic) so the
// backtest-vs-actual chart at the top can be keyed off the row's ticker - every caller
// so far (TopMarketMoverRow, TradingSymbolRow) already satisfies this.
export function TickerDetailsModal<T extends { ticker: string }>({
  row,
  title,
  columns,
  formatCell,
  onClose,
}: TickerDetailsModalProps<T>) {
  type Key = Extract<keyof T, string>

  const initialHiddenRef = useRef<Set<string> | undefined>(undefined)
  if (initialHiddenRef.current === undefined) initialHiddenRef.current = loadHiddenFields()

  const [hiddenKeys, setHiddenKeys] = useState<Set<string>>(() => new Set(initialHiddenRef.current))
  const [justSaved, setJustSaved] = useState(false)
  const savedFlashTimeout = useRef<number | null>(null)
  useEffect(() => () => {
    if (savedFlashTimeout.current) window.clearTimeout(savedFlashTimeout.current)
  }, [])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  const toggleHidden = (key: Key) => {
    setHiddenKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const handleSave = () => {
    saveHiddenFields(hiddenKeys)
    setJustSaved(true)
    if (savedFlashTimeout.current) window.clearTimeout(savedFlashTimeout.current)
    savedFlashTimeout.current = window.setTimeout(() => setJustSaved(false), 1500)
  }

  const handleReset = () => {
    clearHiddenFields()
    setHiddenKeys(new Set())
  }

  const visibleColumns = columns.filter((col) => !hiddenKeys.has(col.key))

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal modal-wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="ticker-details-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header-row">
          <h2 id="ticker-details-title" className="modal-title">
            {title}
          </h2>
          <button type="button" className="icon-button modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <BacktestChart ticker={row.ticker} />

        <div className="detail-modal-toolbar">
          <ColumnsMenu
            columns={columns}
            hiddenKeys={hiddenKeys as Set<Key>}
            onToggle={toggleHidden}
            label="Fields"
            buttonClassName="job-button"
          />
          <button type="button" className="job-button" onClick={handleReset}>
            Reset
          </button>
          <button type="button" className="job-button job-button-primary" onClick={handleSave}>
            {justSaved ? 'Saved' : 'Save settings'}
          </button>
        </div>

        {visibleColumns.length === 0 ? (
          <p className="placeholder-note">Every field is hidden - use "Fields" above to show some.</p>
        ) : (
          <dl className="detail-modal-grid">
            {visibleColumns.map((col) => (
              <div className="detail-modal-item" key={col.key}>
                <dt>{col.label}</dt>
                <dd>{formatCell(row, col.key)}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>
    </div>
  )
}
