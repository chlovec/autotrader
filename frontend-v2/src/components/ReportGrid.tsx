import { createPortal } from 'react-dom'
import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ColumnHeaderMenu } from './ColumnHeaderMenu'
import { useAnchoredDropdown } from './useAnchoredDropdown'

export type ReportColumn<T> = {
  key: Extract<keyof T, string>
  label: string
}

type ReportGridProps<T> = {
  columns: ReportColumn<T>[]
  rows: T[]
  rowKey: (row: T) => string
  formatCell: (row: T, key: Extract<keyof T, string>) => string
  // Shown in place of the grid when `rows` is empty - the report hasn't found
  // anything, as opposed to a column filter hiding everything, which the grid
  // handles itself since only it knows the active filters.
  emptyMessage?: string
}

type SortDir = 'asc' | 'desc'

// Nulls sort last regardless of direction (common spreadsheet convention) rather than
// flipping to the front on desc, which would otherwise scatter blanks through the
// middle of an otherwise-sorted column every other click.
function compareRows<T>(a: T, b: T, key: Extract<keyof T, string>, dir: SortDir): number {
  const sign = dir === 'asc' ? 1 : -1
  const av = a[key]
  const bv = b[key]
  if (av == null && bv == null) return 0
  if (av == null) return 1
  if (bv == null) return -1
  if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sign
  return String(av).localeCompare(String(bv)) * sign
}

const COLUMNS_MENU_WIDTH = 220

// The "Columns" toolbar button - always visible above the grid regardless of which
// columns are currently hidden, since a hidden column's own header (and so its
// ColumnHeaderMenu) disappears along with it. This is the only way back to unhide one.
function ColumnsMenu<T>({
  columns,
  hiddenKeys,
  onToggle,
}: {
  columns: ReportColumn<T>[]
  hiddenKeys: Set<Extract<keyof T, string>>
  onToggle: (key: Extract<keyof T, string>) => void
}) {
  const { open, position, anchorRef, dropdownRef, toggleMenu } = useAnchoredDropdown(COLUMNS_MENU_WIDTH)

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        className={`job-button report-columns-button${hiddenKeys.size > 0 ? ' report-columns-button-active' : ''}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={toggleMenu}
      >
        Columns{hiddenKeys.size > 0 ? ` (${hiddenKeys.size} hidden)` : ''}
      </button>
      {open &&
        position &&
        createPortal(
          <div
            ref={dropdownRef}
            className="report-menu-dropdown"
            role="menu"
            style={{ position: 'fixed', top: position.top, left: position.left, width: COLUMNS_MENU_WIDTH }}
          >
            <div className="report-menu-section-title">Show / hide columns</div>
            <ul className="report-filter-list">
              {columns.map((col) => (
                <li key={col.key}>
                  <label className="report-filter-option">
                    <input type="checkbox" checked={!hiddenKeys.has(col.key)} onChange={() => onToggle(col.key)} />
                    <span>{col.label}</span>
                  </label>
                </li>
              ))}
            </ul>
          </div>,
          document.body,
        )}
    </>
  )
}

// Reusable data grid for the Analytics report pages: sticky/themed header, sort,
// per-column Excel-style value filters, freeze (pin) columns, and show/hide columns -
// all driven by a plain columns/rows/formatCell contract so a new report page just
// supplies its own data instead of reimplementing any of this.
export function ReportGrid<T>({ columns, rows, rowKey, formatCell, emptyMessage }: ReportGridProps<T>) {
  type Key = Extract<keyof T, string>

  const [sortKey, setSortKey] = useState<Key | null>(null)
  const [sortDir, setSortDir] = useState<SortDir>('asc')
  // Missing/null for a column means "everything selected" (no filter) - see
  // ColumnHeaderMenu's `selected` prop.
  const [filters, setFilters] = useState<Partial<Record<Key, Set<string>>>>({})
  const [frozenKeys, setFrozenKeys] = useState<Set<Key>>(new Set())
  const [hiddenKeys, setHiddenKeys] = useState<Set<Key>>(new Set())
  // Pixel offset (sum of the widths of the frozen columns before it, in visible column
  // order) for each currently-frozen column - measured from the live DOM rather than
  // derived from fixed widths, since column widths are content-driven (nowrap cells).
  const [frozenOffsets, setFrozenOffsets] = useState<Partial<Record<Key, number>>>({})
  const thRefs = useRef<Partial<Record<Key, HTMLTableCellElement | null>>>({})

  const visibleColumns = useMemo(() => columns.filter((col) => !hiddenKeys.has(col.key)), [columns, hiddenKeys])

  // Every distinct formatted value per column, computed from the full result (not the
  // filtered/sorted view) so a column's own filter dropdown always offers every value
  // the report returned, not just the ones other columns' filters currently leave
  // visible. Kept for every column, not just visible ones, so unhiding a previously-
  // filtered column doesn't need to recompute anything.
  const columnValues = useMemo(() => {
    const map: Partial<Record<Key, string[]>> = {}
    for (const col of columns) {
      const seen = new Set<string>()
      for (const row of rows) seen.add(formatCell(row, col.key))
      map[col.key] = Array.from(seen).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    }
    return map
  }, [columns, rows, formatCell])

  const filteredRows = useMemo(() => {
    const active = Object.entries(filters) as [Key, Set<string>][]
    if (active.length === 0) return rows
    return rows.filter((row) => active.every(([key, allowed]) => allowed.has(formatCell(row, key))))
  }, [rows, filters, formatCell])

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
      if (next === null) {
        delete copy[key]
      } else {
        copy[key] = next
      }
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

  // Re-measures whenever which columns are frozen/hidden changes, or the visible rows
  // change (filtering/sorting can change a column's longest value, and so its width) -
  // runs before paint so a freshly-frozen column never flashes at the wrong offset.
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

  if (rows.length === 0) {
    return <p className="placeholder-note">{emptyMessage ?? 'No rows found.'}</p>
  }

  return (
    <div className="report-grid">
      <div className="report-grid-toolbar">
        <ColumnsMenu columns={columns} hiddenKeys={hiddenKeys} onToggle={toggleHidden} />
      </div>

      {/* thead renders unconditionally, even when no rows survive the current filters -
          otherwise clearing/deselecting every entry in a column's filter (a "0 rows
          match" state) would take the header, and with it every column's own
          ColumnHeaderMenu, off screen along with the body - leaving no way back into
          that filter's dropdown to re-select anything. */}
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
                      onSortAsc={() => setSort(col.key, 'asc')}
                      onSortDesc={() => setSort(col.key, 'desc')}
                      onToggleFrozen={() => toggleFrozen(col.key)}
                      onHide={() => toggleHidden(col.key)}
                      onFilterChange={(next) => setColumnFilter(col.key, next)}
                    />
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {sortedRows.length === 0 ? (
              <tr>
                <td className="report-empty-cell" colSpan={visibleColumns.length}>
                  No rows match the current column filters.
                </td>
              </tr>
            ) : (
              sortedRows.map((row) => (
                <tr key={rowKey(row)}>
                  {visibleColumns.map((col) => {
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
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
