import { createPortal } from 'react-dom'
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
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
  // Distinct id for this report (e.g. "top-movers") - opts into the "Save view" /
  // "Reset view" toolbar buttons, which persist sort/filter/freeze/hide/width settings
  // to localStorage under this key so re-running the report later (a fresh mount, not
  // just new `rows` on an already-mounted grid) restores them. Omit to leave the grid
  // stateless across mounts, same as before this existed.
  storageKey?: string
}

type SortDir = 'asc' | 'desc'

type SavedView<Key extends string> = {
  sortKey: Key | null
  sortDir: SortDir
  filters: Partial<Record<Key, string[]>>
  frozenKeys: Key[]
  hiddenKeys: Key[]
  columnWidths: Partial<Record<Key, number>>
}

function viewStorageKey(id: string): string {
  return `report-grid-view:${id}`
}

// Sanitizes against the *current* column list rather than trusting the saved JSON
// outright - a report's column set can change across app versions, and a stale key
// saved under an old version shouldn't resurrect a column that no longer exists.
function loadSavedView<Key extends string>(id: string, validKeys: Set<Key>): SavedView<Key> | null {
  try {
    const raw = window.localStorage.getItem(viewStorageKey(id))
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<SavedView<string>>
    const isValidKey = (key: string): key is Key => validKeys.has(key as Key)
    return {
      sortKey: parsed.sortKey && isValidKey(parsed.sortKey) ? (parsed.sortKey as Key) : null,
      sortDir: parsed.sortDir === 'desc' ? 'desc' : 'asc',
      filters: Object.fromEntries(Object.entries(parsed.filters ?? {}).filter(([key]) => isValidKey(key))) as Partial<
        Record<Key, string[]>
      >,
      frozenKeys: (parsed.frozenKeys ?? []).filter(isValidKey) as Key[],
      hiddenKeys: (parsed.hiddenKeys ?? []).filter(isValidKey) as Key[],
      columnWidths: Object.fromEntries(
        Object.entries(parsed.columnWidths ?? {}).filter(([key]) => isValidKey(key)),
      ) as Partial<Record<Key, number>>,
    }
  } catch {
    return null
  }
}

function saveView<Key extends string>(id: string, view: SavedView<Key>): void {
  try {
    window.localStorage.setItem(viewStorageKey(id), JSON.stringify(view))
  } catch {
    // Storage full/unavailable (private browsing, quota) - saving the view is a
    // nice-to-have, not worth surfacing an error over.
  }
}

function clearSavedView(id: string): void {
  try {
    window.localStorage.removeItem(viewStorageKey(id))
  } catch {
    // ignore, same as saveView above
  }
}

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
const MIN_COLUMN_WIDTH = 64

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
export function ReportGrid<T>({ columns, rows, rowKey, formatCell, emptyMessage, storageKey }: ReportGridProps<T>) {
  type Key = Extract<keyof T, string>

  // Computed exactly once, on mount, and cached in a ref rather than a plain const -
  // the lazy useState initializers below each read from it, and those also only run
  // once on mount, so this needs to already be populated by the time they do. Not
  // reactive to a later storageKey/columns change on purpose: this only ever seeds the
  // grid's *initial* state. undefined (not null) is the "not yet computed" sentinel -
  // "no saved view found" is itself a legitimate null result that shouldn't trigger
  // re-reading localStorage on every subsequent render.
  const initialViewRef = useRef<SavedView<Key> | null | undefined>(undefined)
  if (initialViewRef.current === undefined) {
    initialViewRef.current = storageKey ? loadSavedView<Key>(storageKey, new Set(columns.map((col) => col.key))) : null
  }
  const initialView = initialViewRef.current

  const [sortKey, setSortKey] = useState<Key | null>(initialView?.sortKey ?? null)
  const [sortDir, setSortDir] = useState<SortDir>(initialView?.sortDir ?? 'asc')
  // Missing/null for a column means "everything selected" (no filter) - see
  // ColumnHeaderMenu's `selected` prop.
  const [filters, setFilters] = useState<Partial<Record<Key, Set<string>>>>(() => {
    const entries = Object.entries(initialView?.filters ?? {}) as [Key, string[]][]
    return Object.fromEntries(entries.map(([key, values]) => [key, new Set(values)])) as Partial<
      Record<Key, Set<string>>
    >
  })
  const [frozenKeys, setFrozenKeys] = useState<Set<Key>>(new Set(initialView?.frozenKeys ?? []))
  const [hiddenKeys, setHiddenKeys] = useState<Set<Key>>(new Set(initialView?.hiddenKeys ?? []))
  // Pixel offset (sum of the widths of the frozen columns before it, in visible column
  // order) for each currently-frozen column - measured from the live DOM rather than
  // derived from fixed widths, since column widths are content-driven (nowrap cells).
  const [frozenOffsets, setFrozenOffsets] = useState<Partial<Record<Key, number>>>({})
  const thRefs = useRef<Partial<Record<Key, HTMLTableCellElement | null>>>({})
  // Explicit pixel width for a column once the user has dragged its resize handle -
  // unset columns stay auto-sized/nowrap as before. Once set, that column's cells
  // switch to wrapping instead of overflowing (see the report-cell-wrap class below).
  const [columnWidths, setColumnWidths] = useState<Partial<Record<Key, number>>>(initialView?.columnWidths ?? {})
  const resizingRef = useRef<{ key: Key; startX: number; startWidth: number } | null>(null)

  const [justSaved, setJustSaved] = useState(false)
  const savedFlashTimeout = useRef<number | null>(null)
  useEffect(() => () => {
    if (savedFlashTimeout.current) window.clearTimeout(savedFlashTimeout.current)
  }, [])

  const handleSaveView = () => {
    if (!storageKey) return
    saveView<Key>(storageKey, {
      sortKey,
      sortDir,
      filters: Object.fromEntries(
        Object.entries(filters).map(([key, values]) => [key, Array.from(values as Set<string>)]),
      ) as Partial<Record<Key, string[]>>,
      frozenKeys: Array.from(frozenKeys),
      hiddenKeys: Array.from(hiddenKeys),
      columnWidths,
    })
    setJustSaved(true)
    if (savedFlashTimeout.current) window.clearTimeout(savedFlashTimeout.current)
    savedFlashTimeout.current = window.setTimeout(() => setJustSaved(false), 1500)
  }

  const handleResetView = () => {
    if (!storageKey) return
    clearSavedView(storageKey)
    setSortKey(null)
    setSortDir('asc')
    setFilters({})
    setFrozenKeys(new Set())
    setHiddenKeys(new Set())
    setColumnWidths({})
  }

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

  // Re-measures whenever which columns are frozen/hidden changes, a column is resized,
  // or the visible rows change (filtering/sorting can change a column's longest value,
  // and so its width) - runs before paint so a freshly-frozen column never flashes at
  // the wrong offset.
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
  }, [frozenKeys, visibleColumns, sortedRows, columnWidths])

  // Drag-to-resize: mousedown on a column's resize handle starts tracking, then
  // mousemove/mouseup are wired to the document (not the handle itself) so the drag
  // keeps working even once the cursor leaves the thin handle strip.
  const startResize = (event: ReactMouseEvent, key: Key) => {
    event.preventDefault()
    const startWidth = columnWidths[key] ?? thRefs.current[key]?.getBoundingClientRect().width ?? MIN_COLUMN_WIDTH
    resizingRef.current = { key, startX: event.clientX, startWidth }

    const handleMove = (moveEvent: MouseEvent) => {
      const active = resizingRef.current
      if (!active) return
      const width = Math.max(MIN_COLUMN_WIDTH, active.startWidth + (moveEvent.clientX - active.startX))
      setColumnWidths((prev) => ({ ...prev, [active.key]: width }))
    }
    const stopResize = () => {
      resizingRef.current = null
      document.removeEventListener('mousemove', handleMove)
      document.removeEventListener('mouseup', stopResize)
    }
    document.addEventListener('mousemove', handleMove)
    document.addEventListener('mouseup', stopResize)
  }

  if (rows.length === 0) {
    return <p className="placeholder-note">{emptyMessage ?? 'No rows found.'}</p>
  }

  return (
    <div className="report-grid">
      <div className="report-grid-toolbar">
        {storageKey && (
          <>
            <button type="button" className="job-button" onClick={handleResetView}>
              Reset view
            </button>
            <button type="button" className="job-button" onClick={handleSaveView}>
              {justSaved ? 'Saved' : 'Save view'}
            </button>
          </>
        )}
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
                const width = columnWidths[col.key]
                return (
                  <th
                    key={col.key}
                    ref={(el) => {
                      thRefs.current[col.key] = el
                    }}
                    aria-sort={isSorted ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
                    className={isFrozen ? 'report-cell-frozen-header' : undefined}
                    style={{
                      ...(isFrozen ? { left: frozenOffsets[col.key] ?? 0 } : undefined),
                      ...(width ? { width, minWidth: width, maxWidth: width } : undefined),
                    }}
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
                    {/* mousedown-only drag target - not a <button>, so it doesn't also
                        register as a click on ColumnHeaderMenu's own trigger next to it. */}
                    <span
                      className="report-th-resize-handle"
                      role="separator"
                      aria-orientation="vertical"
                      aria-label={`Resize ${col.label} column`}
                      onMouseDown={(event) => startResize(event, col.key)}
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
                    const width = columnWidths[col.key]
                    return (
                      <td
                        key={col.key}
                        className={
                          [isFrozen && 'report-cell-frozen-body', width && 'report-cell-wrap']
                            .filter(Boolean)
                            .join(' ') || undefined
                        }
                        style={{
                          ...(isFrozen ? { left: frozenOffsets[col.key] ?? 0 } : undefined),
                          ...(width ? { width, minWidth: width, maxWidth: width } : undefined),
                        }}
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
