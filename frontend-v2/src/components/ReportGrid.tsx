import { createPortal } from 'react-dom'
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { isNumericFilterOp, matchesCondition, type NumericCondition } from '../numericFilter'
import { ColumnHeaderMenu } from './ColumnHeaderMenu'
import { useAnchoredDropdown } from './useAnchoredDropdown'

export type ReportColumn<T> = {
  key: Extract<keyof T, string>
  label: string
}

export type RowMenuItem<T> = {
  label: string
  onSelect: (row: T) => void
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
  // Right-click menu shown for any row, e.g. [{ label: 'View Details', onSelect: ... }] -
  // omit to leave rows without a context menu (the browser's native one still shows).
  rowContextMenu?: RowMenuItem<T>[]
}

type SortDir = 'asc' | 'desc'

type SavedView<Key extends string> = {
  sortKey: Key | null
  sortDir: SortDir
  filters: Partial<Record<Key, string[]>>
  numericFilters: Partial<Record<Key, NumericCondition[]>>
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
      numericFilters: Object.fromEntries(
        Object.entries(parsed.numericFilters ?? {})
          .filter(([key]) => isValidKey(key))
          .map(([key, conditions]) => [
            key,
            (conditions ?? []).filter(
              (c): c is NumericCondition => isNumericFilterOp(c?.op) && typeof c?.value === 'number' && Number.isFinite(c.value),
            ),
          ])
          .filter(([, conditions]) => (conditions as NumericCondition[]).length > 0),
      ) as Partial<Record<Key, NumericCondition[]>>,
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
const ROW_MENU_WIDTH = 180

// Row-window virtualization: only the rows actually scrolled into view (plus this many
// on either side, so a fast scroll doesn't flash blank space before the next render
// catches up) are ever mounted as real <tr> elements - the rest of the table's height
// is held open by two spacer rows. Without this, a large report (e.g. Trading Symbols'
// ~46k tickers) would mount every row's cells as real DOM nodes up front regardless of
// how few are ever actually visible through the scrolled viewport.
const OVERSCAN_ROWS = 8
// Matches .report-table td's 9px vertical padding plus its ~19px line height - only
// used for the very first render, before a real row has been measured (see
// measureRowHeight below); wrong by a few px here just means the first paint's window
// is slightly mis-sized until that measurement lands, not a lasting error.
const DEFAULT_ROW_HEIGHT = 37

// The "Columns" toolbar button - always visible above the grid regardless of which
// columns are currently hidden, since a hidden column's own header (and so its
// ColumnHeaderMenu) disappears along with it. This is the only way back to unhide one.
// Exported (and given a configurable `label`/`buttonClassName`) so TickerDetailsModal
// can reuse the exact same show/hide-checklist dropdown for its "Fields" toggle rather
// than reimplementing it.
export function ColumnsMenu<T>({
  columns,
  hiddenKeys,
  onToggle,
  label = 'Columns',
  buttonClassName = 'job-button report-columns-button',
}: {
  columns: ReportColumn<T>[]
  hiddenKeys: Set<Extract<keyof T, string>>
  onToggle: (key: Extract<keyof T, string>) => void
  label?: string
  buttonClassName?: string
}) {
  const { open, position, anchorRef, dropdownRef, toggleMenu } = useAnchoredDropdown(COLUMNS_MENU_WIDTH)

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        className={`${buttonClassName}${hiddenKeys.size > 0 ? ' report-columns-button-active' : ''}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={toggleMenu}
      >
        {label}
        {hiddenKeys.size > 0 ? ` (${hiddenKeys.size} hidden)` : ''}
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
            <div className="report-menu-section-title">Show / hide {label.toLowerCase()}</div>
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
export function ReportGrid<T>({
  columns,
  rows,
  rowKey,
  formatCell,
  emptyMessage,
  storageKey,
  rowContextMenu,
}: ReportGridProps<T>) {
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
  // Missing/empty for a column means "no condition applied" - numeric columns only (see
  // numericKeys below); a value here is always ANDed with that column's value-set filter
  // above if both happen to be set, and multiple conditions on the same column (e.g. >=10
  // and <=50) are ANDed with each other, giving a range.
  const [numericFilters, setNumericFilters] = useState<Partial<Record<Key, NumericCondition[]>>>(
    initialView?.numericFilters ?? {},
  )
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

  // Right-click row menu - positioned at the cursor (unlike ColumnsMenu/
  // ColumnHeaderMenu's useAnchoredDropdown, which anchors to a trigger button's
  // getBoundingClientRect) since there's no button element to anchor to here.
  const [rowMenu, setRowMenu] = useState<{ row: T; top: number; left: number } | null>(null)
  const rowMenuRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!rowMenu) return
    const close = (event: Event) => {
      const target = event.target as Node
      if (rowMenuRef.current?.contains(target)) return
      setRowMenu(null)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setRowMenu(null)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('scroll', close, true)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('scroll', close, true)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [rowMenu])

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
      numericFilters,
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
    setNumericFilters({})
    setFrozenKeys(new Set())
    setHiddenKeys(new Set())
    setColumnWidths({})
  }

  const visibleColumns = useMemo(() => columns.filter((col) => !hiddenKeys.has(col.key)), [columns, hiddenKeys])

  // A column counts as numeric when every non-null raw value seen for it (across the
  // full result, same reasoning as columnValues below) is a number - gates whether
  // ColumnHeaderMenu offers the </<=/>/>= condition filter for it, alongside (not
  // instead of) the value checklist every column already gets.
  const numericKeys = useMemo(() => {
    const set = new Set<Key>()
    for (const col of columns) {
      let sawNumber = false
      let sawNonNumber = false
      for (const row of rows) {
        const value = row[col.key]
        if (value == null) continue
        if (typeof value === 'number') sawNumber = true
        else {
          sawNonNumber = true
          break
        }
      }
      if (sawNumber && !sawNonNumber) set.add(col.key)
    }
    return set
  }, [columns, rows])

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
    const activeValues = Object.entries(filters) as [Key, Set<string>][]
    const activeNumeric = (Object.entries(numericFilters) as [Key, NumericCondition[]][]).filter(
      ([, conditions]) => conditions.length > 0,
    )
    if (activeValues.length === 0 && activeNumeric.length === 0) return rows
    return rows.filter((row) => {
      if (!activeValues.every(([key, allowed]) => allowed.has(formatCell(row, key)))) return false
      return activeNumeric.every(([key, conditions]) => {
        const value = row[key]
        return typeof value === 'number' && conditions.every((condition) => matchesCondition(value, condition))
      })
    })
  }, [rows, filters, numericFilters, formatCell])

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

  const setNumericColumnFilter = (key: Key, next: NumericCondition[] | null) => {
    setNumericFilters((prev) => {
      const copy = { ...prev }
      if (next === null || next.length === 0) {
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

  // Virtualization: scrollTop/viewportHeight of the scrolled .report-table-wrap, kept
  // in state (rather than read ad hoc) since both feed directly into which row indices
  // get windowed into real <tr>s below.
  const scrollElRef = useRef<HTMLDivElement | null>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [viewportHeight, setViewportHeight] = useState(0)
  // Measured once from the first real rendered row rather than assumed - a row's height
  // is uniform in practice (.report-table td is nowrap by default, see index.css) except
  // once a column's been manually resized to wrap (report-cell-wrap), which this can't
  // account for, but that's a rare, user-initiated edge case not worth the complexity of
  // tracking per-row heights for.
  const [measuredRowHeight, setMeasuredRowHeight] = useState<number | null>(null)
  const rowHeight = measuredRowHeight ?? DEFAULT_ROW_HEIGHT
  const measureFirstRow = (el: HTMLTableRowElement | null) => {
    if (el && measuredRowHeight === null) {
      const { height } = el.getBoundingClientRect()
      if (height) setMeasuredRowHeight(height)
    }
  }

  // Tracks the wrap's height as the window/layout changes (e.g. sidebar collapse) -
  // ResizeObserver rather than a window resize listener since the wrap's own size can
  // change independently of the window (e.g. a browser zoom change, or this page's
  // layout shifting for reasons unrelated to the viewport).
  useLayoutEffect(() => {
    const el = scrollElRef.current
    if (!el) return
    setViewportHeight(el.clientHeight)
    const observer = new ResizeObserver(([entry]) => setViewportHeight(entry.contentRect.height))
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  // rAF-throttled rather than set on every scroll event - a fast scroll/trackpad fling
  // can fire dozens of scroll events per frame, and each one only needs to result in at
  // most one re-render.
  const scrollRafRef = useRef<number | null>(null)
  useEffect(() => () => {
    if (scrollRafRef.current != null) cancelAnimationFrame(scrollRafRef.current)
  }, [])
  const handleScroll = () => {
    if (scrollRafRef.current != null) return
    scrollRafRef.current = requestAnimationFrame(() => {
      scrollRafRef.current = null
      const el = scrollElRef.current
      if (el) setScrollTop(el.scrollTop)
    })
  }

  const rowCount = sortedRows.length
  const startIndex = Math.max(0, Math.floor(scrollTop / rowHeight) - OVERSCAN_ROWS)
  const endIndex = Math.min(rowCount, Math.ceil((scrollTop + viewportHeight) / rowHeight) + OVERSCAN_ROWS)
  const visibleRows = sortedRows.slice(startIndex, endIndex)
  const topSpacerHeight = startIndex * rowHeight
  const bottomSpacerHeight = (rowCount - endIndex) * rowHeight

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
      <div className="report-table-wrap" ref={scrollElRef} onScroll={handleScroll}>
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
                      numeric={numericKeys.has(col.key)}
                      numericConditions={numericFilters[col.key] ?? null}
                      onSortAsc={() => setSort(col.key, 'asc')}
                      onSortDesc={() => setSort(col.key, 'desc')}
                      onToggleFrozen={() => toggleFrozen(col.key)}
                      onHide={() => toggleHidden(col.key)}
                      onFilterChange={(next) => setColumnFilter(col.key, next)}
                      onNumericFilterChange={(next) => setNumericColumnFilter(col.key, next)}
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
              <>
                {/* Holds open the scrolled height of every row above the windowed slice,
                    so the real rows below sit at the same scroll offset they would if
                    every row between them and the top were actually mounted. */}
                {topSpacerHeight > 0 && (
                  <tr aria-hidden="true" style={{ height: topSpacerHeight }}>
                    <td colSpan={visibleColumns.length} style={{ padding: 0, border: 'none' }} />
                  </tr>
                )}
                {visibleRows.map((row, i) => (
                  <tr
                    key={rowKey(row)}
                    ref={i === 0 ? measureFirstRow : undefined}
                    onContextMenu={
                      rowContextMenu && rowContextMenu.length > 0
                        ? (event) => {
                            event.preventDefault()
                            const left = Math.min(event.clientX, window.innerWidth - ROW_MENU_WIDTH - 4)
                            setRowMenu({ row, top: event.clientY, left })
                          }
                        : undefined
                    }
                  >
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
                ))}
                {/* Same reasoning as the top spacer, for every row below the windowed
                    slice - keeps the scrollbar's total scrollable height correct. */}
                {bottomSpacerHeight > 0 && (
                  <tr aria-hidden="true" style={{ height: bottomSpacerHeight }}>
                    <td colSpan={visibleColumns.length} style={{ padding: 0, border: 'none' }} />
                  </tr>
                )}
              </>
            )}
          </tbody>
        </table>
      </div>

      {rowMenu &&
        rowContextMenu &&
        createPortal(
          <div
            ref={rowMenuRef}
            className="report-menu-dropdown"
            role="menu"
            style={{ position: 'fixed', top: rowMenu.top, left: rowMenu.left, width: ROW_MENU_WIDTH }}
          >
            {rowContextMenu.map((item) => (
              <button
                key={item.label}
                type="button"
                className="report-menu-item"
                onClick={() => {
                  item.onSelect(rowMenu.row)
                  setRowMenu(null)
                }}
              >
                {item.label}
              </button>
            ))}
          </div>,
          document.body,
        )}
    </div>
  )
}
