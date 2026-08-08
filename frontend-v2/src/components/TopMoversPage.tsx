import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { api, type TickerTypeOption, type TopMarketMoverRow } from '../api'
import { ColumnHeaderMenu } from './ColumnHeaderMenu'
import { SearchableSelect, type SelectOption } from './SearchableSelect'

// Backend caps /ticker-types/search's limit at 50 (see app/main.py's search_ticker_types) -
// ticker_types is a short, mostly-static reference list (db/models.py's TickerType
// docstring), so that's comfortably the whole thing in one page.
const TICKER_TYPE_OPTIONS_LIMIT = 50

function tickerTypeLabel(t: TickerTypeOption): string {
  const detail = [t.asset_class, t.description].filter(Boolean).join(': ')
  return detail ? `${t.code} — ${detail}` : t.code
}

type Column = { key: keyof TopMarketMoverRow; label: string }

const COLUMNS: Column[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'type', label: 'Type' },
  { key: 'asset_class', label: 'Asset Class' },
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
]

// updated/min_timestamp/fetched_at are naive-UTC (same as JobRun.started_at) - append
// "Z" so Date parses them as UTC instead of local time, same reasoning as JobCard's
// formatTimestamp.
const TIMESTAMP_FIELDS = new Set<keyof TopMarketMoverRow>(['updated', 'min_timestamp', 'fetched_at'])

function formatCell(row: TopMarketMoverRow, key: keyof TopMarketMoverRow): string {
  const value = row[key]
  if (value == null) return '–'
  if (TIMESTAMP_FIELDS.has(key)) return new Date(`${value}Z`).toLocaleString()
  if (typeof value === 'number') return value.toLocaleString(undefined, { maximumFractionDigits: 4 })
  return String(value)
}

type SortDir = 'asc' | 'desc'

// Nulls sort last regardless of direction (common spreadsheet convention) rather than
// flipping to the front on desc, which would otherwise scatter blanks through the
// middle of an otherwise-sorted column every other click.
function compareRows(a: TopMarketMoverRow, b: TopMarketMoverRow, key: keyof TopMarketMoverRow, dir: SortDir): number {
  const sign = dir === 'asc' ? 1 : -1
  const av = a[key]
  const bv = b[key]
  if (av == null && bv == null) return 0
  if (av == null) return 1
  if (bv == null) return -1
  if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sign
  return String(av).localeCompare(String(bv)) * sign
}

export function TopMoversPage() {
  const [tickerTypeOptions, setTickerTypeOptions] = useState<SelectOption[]>([])
  const [tickerTypes, setTickerTypes] = useState<string[]>([])
  const [rows, setRows] = useState<TopMarketMoverRow[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sortKey, setSortKey] = useState<keyof TopMarketMoverRow | null>(null)
  const [sortDir, setSortDir] = useState<SortDir>('asc')
  // Missing/null for a column means "everything selected" (no filter) - see
  // ColumnHeaderMenu's `selected` prop.
  const [filters, setFilters] = useState<Partial<Record<keyof TopMarketMoverRow, Set<string>>>>({})
  const [frozenKeys, setFrozenKeys] = useState<Set<keyof TopMarketMoverRow>>(new Set())
  // Pixel offset (sum of the widths of the frozen columns before it, in column order)
  // for each currently-frozen column - measured from the live DOM rather than derived
  // from fixed widths, since column widths are content-driven (nowrap cells).
  const [frozenOffsets, setFrozenOffsets] = useState<Partial<Record<keyof TopMarketMoverRow, number>>>({})
  const thRefs = useRef<Partial<Record<keyof TopMarketMoverRow, HTMLTableCellElement | null>>>({})

  // Every distinct formatted value per column, computed from the full fetched result
  // (not the filtered/sorted view) so a column's own filter dropdown always offers
  // every value that came back from the report, not just the ones other columns'
  // filters currently leave visible.
  const columnValues = useMemo<Partial<Record<keyof TopMarketMoverRow, string[]>>>(() => {
    if (!rows) return {}
    const map: Partial<Record<keyof TopMarketMoverRow, string[]>> = {}
    for (const col of COLUMNS) {
      const seen = new Set<string>()
      for (const row of rows) seen.add(formatCell(row, col.key))
      map[col.key] = Array.from(seen).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    }
    return map
  }, [rows])

  const filteredRows = useMemo(() => {
    if (!rows) return rows
    const active = Object.entries(filters) as [keyof TopMarketMoverRow, Set<string>][]
    if (active.length === 0) return rows
    return rows.filter((row) => active.every(([key, allowed]) => allowed.has(formatCell(row, key))))
  }, [rows, filters])

  const sortedRows = useMemo(() => {
    if (!filteredRows || !sortKey) return filteredRows
    return [...filteredRows].sort((a, b) => compareRows(a, b, sortKey, sortDir))
  }, [filteredRows, sortKey, sortDir])

  const setSort = (key: keyof TopMarketMoverRow, dir: SortDir) => {
    setSortKey(key)
    setSortDir(dir)
  }

  const setColumnFilter = (key: keyof TopMarketMoverRow, next: Set<string> | null) => {
    setFilters((prev) => {
      const copy = { ...prev }
      if (next === null) {
        delete copy[key]
      } else {
        copy[key] = next
      }
      return copy
    })
  }

  const toggleFrozen = (key: keyof TopMarketMoverRow) => {
    setFrozenKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  // Re-measures whenever which columns are frozen changes, or the visible rows change
  // (filtering/sorting can change a column's longest value, and so its width) - runs
  // before paint so a freshly-frozen column never flashes at the wrong offset.
  useLayoutEffect(() => {
    if (frozenKeys.size === 0) {
      setFrozenOffsets({})
      return
    }
    const measure = () => {
      let cumulative = 0
      const next: Partial<Record<keyof TopMarketMoverRow, number>> = {}
      for (const col of COLUMNS) {
        if (!frozenKeys.has(col.key)) continue
        next[col.key] = cumulative
        cumulative += thRefs.current[col.key]?.getBoundingClientRect().width ?? 0
      }
      setFrozenOffsets(next)
    }
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [frozenKeys, sortedRows])

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
      // A fresh result set can drop values a previous filter referenced, so start
      // clean rather than carry filters that might now hide everything silently.
      setFilters({})
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load report')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="report-page">
      <h1 className="jobs-page-title">Top Movers</h1>
      <p className="jobs-page-subtitle">
        Today's top-gaining and top-losing tickers. Optionally filter by ticker type before running.
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
        <button type="button" className="job-button job-button-primary" disabled={loading} onClick={runReport}>
          {loading ? 'Running...' : 'Run report'}
        </button>
      </div>

      {error && <p className="jobs-error">{error}</p>}

      {!loading && rows && rows.length === 0 && <p className="placeholder-note">No movers found.</p>}

      {!loading && rows && rows.length > 0 && filteredRows && filteredRows.length === 0 && (
        <p className="placeholder-note">No rows match the current column filters.</p>
      )}

      {!loading && filteredRows && filteredRows.length > 0 && (
        <div className="report-table-wrap">
          <table className="job-history-table report-table">
            <thead>
              <tr>
                {COLUMNS.map((col) => {
                  const isSorted = sortKey === col.key
                  const isFrozen = frozenKeys.has(col.key)
                  return (
                    <th
                      key={col.key}
                      ref={(el) => {
                        thRefs.current[col.key] = el
                      }}
                      aria-sort={isSorted ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
                      className={isFrozen ? 'report-cell-frozen-header' : undefined}
                      style={isFrozen ? { left: frozenOffsets[col.key] ?? 0 } : undefined}
                    >
                      <ColumnHeaderMenu
                        label={col.label}
                        sortDir={isSorted ? sortDir : null}
                        frozen={isFrozen}
                        values={columnValues[col.key] ?? []}
                        selected={filters[col.key] ?? null}
                        onSortAsc={() => setSort(col.key, 'asc')}
                        onSortDesc={() => setSort(col.key, 'desc')}
                        onToggleFrozen={() => toggleFrozen(col.key)}
                        onFilterChange={(next) => setColumnFilter(col.key, next)}
                      />
                    </th>
                  )
                })}
              </tr>
            </thead>
            <tbody>
              {(sortedRows ?? []).map((row) => (
                <tr key={`${row.ticker}-${row.direction}`}>
                  {COLUMNS.map((col) => {
                    const isFrozen = frozenKeys.has(col.key)
                    return (
                      <td
                        key={col.key}
                        className={isFrozen ? 'report-cell-frozen-body' : undefined}
                        style={isFrozen ? { left: frozenOffsets[col.key] ?? 0 } : undefined}
                      >
                        {formatCell(row, col.key)}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!rows && !loading && !error && (
        <p className="placeholder-note">Choose ticker types (optional) and run the report.</p>
      )}
    </div>
  )
}
