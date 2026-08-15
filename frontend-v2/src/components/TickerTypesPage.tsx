import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { api, type TickerTypeRow, type TickerTypeStatus } from '../api'
import { matchesCondition, type NumericCondition } from '../numericFilter'
import { ColumnHeaderMenu } from './ColumnHeaderMenu'
import { ColumnsMenu, type ReportColumn } from './ReportGrid'

type Key = Extract<keyof TickerTypeRow, string>
type SortDir = 'asc' | 'desc'

function rowKey(row: Pick<TickerTypeRow, 'code' | 'asset_class' | 'locale'>): string {
  return `${row.code}|${row.asset_class}|${row.locale}`
}

// Local edit buffer for one row - separate from the fetched TickerTypeRow so a saved
// row's actual values (source of truth) aren't overwritten until the PUT succeeds.
type Edit = { rank: string; status: TickerTypeStatus }

const COLUMNS: ReportColumn<TickerTypeRow>[] = [
  { key: 'code', label: 'Code' },
  { key: 'asset_class', label: 'Asset Class' },
  { key: 'locale', label: 'Locale' },
  { key: 'description', label: 'Description' },
  { key: 'rank', label: 'Rank' },
  { key: 'status', label: 'Status' },
]

// Only 'rank' carries a genuinely numeric value (number | null) - every other column is
// a plain string, so unlike ReportGrid's numericKeys (computed generically from
// whatever rows happen to come back) this is just hardcoded against the known schema.
const NUMERIC_KEYS: Set<Key> = new Set(['rank'])

function formatCell(row: TickerTypeRow, key: Key): string {
  if (key === 'rank') return row.rank == null ? 'Unranked' : String(row.rank)
  if (key === 'status') return row.status === 'active' ? 'Active' : 'Inactive'
  if (key === 'description') return row.description ?? '—'
  return String(row[key])
}

// Same "nulls sort last regardless of direction" convention as ReportGrid's own
// compareRows (not exported from there, so duplicated here rather than reaching into
// another component's internals for a ~10-line helper).
function compareRows(a: TickerTypeRow, b: TickerTypeRow, key: Key, dir: SortDir): number {
  const sign = dir === 'asc' ? 1 : -1
  const av = a[key]
  const bv = b[key]
  if (av == null && bv == null) return 0
  if (av == null) return 1
  if (bv == null) return -1
  if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sign
  return String(av).localeCompare(String(bv)) * sign
}

export function TickerTypesPage() {
  const [rows, setRows] = useState<TickerTypeRow[] | null>(null)
  const [edits, setEdits] = useState<Record<string, Edit>>({})
  const [saving, setSaving] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Sort/filter/freeze/hide - same shape and behavior as ReportGrid's own state, just
  // hand-rolled here rather than reusing ReportGrid wholesale, since ReportGrid's cells
  // are plain formatCell strings with no way to render this page's editable rank/status
  // inputs.
  const [sortKey, setSortKey] = useState<Key | null>(null)
  const [sortDir, setSortDir] = useState<SortDir>('asc')
  const [filters, setFilters] = useState<Partial<Record<Key, Set<string>>>>({})
  const [numericFilters, setNumericFilters] = useState<Partial<Record<Key, NumericCondition[]>>>({})
  const [frozenKeys, setFrozenKeys] = useState<Set<Key>>(new Set())
  const [hiddenKeys, setHiddenKeys] = useState<Set<Key>>(new Set())
  const [frozenOffsets, setFrozenOffsets] = useState<Partial<Record<Key, number>>>({})
  const thRefs = useRef<Partial<Record<Key, HTMLTableCellElement | null>>>({})

  const load = () => {
    setLoading(true)
    setError(null)
    api
      .tickerTypes()
      .then((data) => {
        setRows(data)
        setEdits(Object.fromEntries(data.map((row) => [rowKey(row), { rank: row.rank == null ? '' : String(row.rank), status: row.status }])))
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load ticker types'))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  const setEdit = (key: string, next: Partial<Edit>) => {
    setEdits((prev) => ({ ...prev, [key]: { ...prev[key], ...next } }))
  }

  const isDirty = (row: TickerTypeRow): boolean => {
    const edit = edits[rowKey(row)]
    if (!edit) return false
    const editedRank = edit.rank.trim() === '' ? null : Number(edit.rank)
    return editedRank !== row.rank || edit.status !== row.status
  }

  const save = async (row: TickerTypeRow) => {
    const key = rowKey(row)
    const edit = edits[key]
    if (!edit) return
    const trimmed = edit.rank.trim()
    const rank = trimmed === '' ? null : Number(trimmed)
    if (rank !== null && (!Number.isFinite(rank) || rank < 1)) {
      setError('Rank must be a positive number, or blank for unranked.')
      return
    }
    setError(null)
    setSaving((prev) => new Set(prev).add(key))
    try {
      const updated = await api.updateTickerType(row.code, row.asset_class, row.locale, { rank, status: edit.status })
      setRows((prev) => prev?.map((r) => (rowKey(r) === key ? updated : r)) ?? prev)
      setEdits((prev) => ({ ...prev, [key]: { rank: updated.rank == null ? '' : String(updated.rank), status: updated.status } }))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save ticker type')
    } finally {
      setSaving((prev) => {
        const next = new Set(prev)
        next.delete(key)
        return next
      })
    }
  }

  const visibleColumns = useMemo(() => COLUMNS.filter((col) => !hiddenKeys.has(col.key)), [hiddenKeys])

  // Every distinct formatted value per column, from the full unfiltered `rows` - same
  // reasoning as ReportGrid's columnValues, so a column's filter checklist always
  // offers every value regardless of what other columns' filters currently hide.
  const columnValues = useMemo(() => {
    const map: Partial<Record<Key, string[]>> = {}
    const source = rows ?? []
    for (const col of COLUMNS) {
      const seen = new Set<string>()
      for (const row of source) seen.add(formatCell(row, col.key))
      map[col.key] = Array.from(seen).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    }
    return map
  }, [rows])

  const filteredRows = useMemo(() => {
    const source = rows ?? []
    const activeValues = Object.entries(filters) as [Key, Set<string>][]
    const activeNumeric = (Object.entries(numericFilters) as [Key, NumericCondition[]][]).filter(
      ([, conditions]) => conditions.length > 0,
    )
    if (activeValues.length === 0 && activeNumeric.length === 0) return source
    return source.filter((row) => {
      if (!activeValues.every(([key, allowed]) => allowed.has(formatCell(row, key)))) return false
      return activeNumeric.every(([key, conditions]) => {
        const value = row[key]
        return typeof value === 'number' && conditions.every((condition) => matchesCondition(value, condition))
      })
    })
  }, [rows, filters, numericFilters])

  const sortedRows = useMemo(() => {
    if (!sortKey) return filteredRows
    return [...filteredRows].sort((a, b) => compareRows(a, b, sortKey, sortDir))
  }, [filteredRows, sortKey, sortDir])

  const setSort = (key: Key, dir: SortDir) => {
    setSortKey(key)
    setSortDir(dir)
  }

  const setColumnFilter = (key: Key, next: Set<string> | null) => {
    setFilters((prev) => {
      const copy = { ...prev }
      if (next === null) delete copy[key]
      else copy[key] = next
      return copy
    })
  }

  const setNumericColumnFilter = (key: Key, next: NumericCondition[] | null) => {
    setNumericFilters((prev) => {
      const copy = { ...prev }
      if (next === null || next.length === 0) delete copy[key]
      else copy[key] = next
      return copy
    })
  }

  const toggleFrozen = (key: Key) => {
    setFrozenKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const toggleHidden = (key: Key) => {
    setHiddenKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  // Same measurement approach as ReportGrid - pixel offset of each frozen column from
  // the sum of frozen columns' widths before it, re-measured whenever what's
  // frozen/hidden/visible changes.
  useLayoutEffect(() => {
    if (frozenKeys.size === 0) {
      setFrozenOffsets({})
      return
    }
    const measure = () => {
      let cumulative = 0
      const next: Partial<Record<Key, number>> = {}
      for (const col of visibleColumns) {
        if (!frozenKeys.has(col.key)) continue
        next[col.key] = cumulative
        cumulative += thRefs.current[col.key]?.getBoundingClientRect().width ?? 0
      }
      setFrozenOffsets(next)
    }
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [frozenKeys, visibleColumns, sortedRows])

  return (
    <div className="report-page">
      <h1 className="jobs-page-title">Ticker Types</h1>
      <p className="jobs-page-subtitle">
        Set a display rank and active/inactive status for each ticker type synced from massive.com. Unranked types
        sort last.
      </p>

      {error && <p className="jobs-error">{error}</p>}

      {loading && <p className="placeholder-note">Loading ticker types...</p>}

      {!loading && rows && rows.length === 0 && <p className="placeholder-note">No ticker types synced yet.</p>}

      {!loading && rows && rows.length > 0 && (
        <div className="report-grid">
          <div className="report-grid-toolbar">
            <ColumnsMenu columns={COLUMNS} hiddenKeys={hiddenKeys} onToggle={toggleHidden} />
          </div>
          <div className="report-table-wrap">
            <table className="job-history-table report-table">
              <thead>
                <tr>
                  {visibleColumns.map((col) => {
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
                          numeric={NUMERIC_KEYS.has(col.key)}
                          numericConditions={numericFilters[col.key] ?? null}
                          onSortAsc={() => setSort(col.key, 'asc')}
                          onSortDesc={() => setSort(col.key, 'desc')}
                          onToggleFrozen={() => toggleFrozen(col.key)}
                          onHide={() => toggleHidden(col.key)}
                          onFilterChange={(next) => setColumnFilter(col.key, next)}
                          onNumericFilterChange={(next) => setNumericColumnFilter(col.key, next)}
                        />
                      </th>
                    )
                  })}
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {sortedRows.length === 0 ? (
                  <tr>
                    <td className="report-empty-cell" colSpan={visibleColumns.length + 1}>
                      No rows match the current column filters.
                    </td>
                  </tr>
                ) : (
                  sortedRows.map((row) => {
                    const key = rowKey(row)
                    const edit = edits[key] ?? { rank: '', status: row.status }
                    const dirty = isDirty(row)
                    const isSaving = saving.has(key)
                    return (
                      <tr key={key}>
                        {visibleColumns.map((col) => {
                          const isFrozen = frozenKeys.has(col.key)
                          const style = isFrozen ? { left: frozenOffsets[col.key] ?? 0 } : undefined
                          const className = isFrozen ? 'report-cell-frozen-body' : undefined
                          if (col.key === 'rank') {
                            return (
                              <td key={col.key} className={className} style={style}>
                                <input
                                  type="number"
                                  min={1}
                                  step={1}
                                  value={edit.rank}
                                  placeholder="Unranked"
                                  onChange={(event) => setEdit(key, { rank: event.target.value })}
                                  style={{ width: '90px' }}
                                />
                              </td>
                            )
                          }
                          if (col.key === 'status') {
                            return (
                              <td key={col.key} className={className} style={style}>
                                <select
                                  value={edit.status}
                                  onChange={(event) => setEdit(key, { status: event.target.value as TickerTypeStatus })}
                                >
                                  <option value="active">Active</option>
                                  <option value="inactive">Inactive</option>
                                </select>
                              </td>
                            )
                          }
                          return (
                            <td key={col.key} className={className} style={style}>
                              {formatCell(row, col.key)}
                            </td>
                          )
                        })}
                        <td>
                          <button
                            type="button"
                            className="job-button job-button-primary"
                            disabled={!dirty || isSaving}
                            onClick={() => save(row)}
                          >
                            {isSaving ? 'Saving...' : 'Save'}
                          </button>
                        </td>
                      </tr>
                    )
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
