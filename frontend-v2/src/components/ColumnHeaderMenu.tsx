import { createPortal } from 'react-dom'
import { useEffect, useRef, useState } from 'react'
import { useAnchoredDropdown } from './useAnchoredDropdown'

type ColumnHeaderMenuProps = {
  label: string
  sortDir: 'asc' | 'desc' | null // null - this isn't the active sort column
  frozen: boolean
  // Every distinct value present in the column, unfiltered - the dropdown's full
  // checklist regardless of what's currently selected.
  values: string[]
  // null means "everything selected" (no filter applied) rather than an explicit set
  // equal to `values` - keeps the common no-filter case cheap to check elsewhere
  // (TopMoversPage's filteredRows) without comparing set contents.
  selected: Set<string> | null
  onSortAsc: () => void
  onSortDesc: () => void
  onToggleFrozen: () => void
  onHide: () => void
  onFilterChange: (next: Set<string> | null) => void
}

const DROPDOWN_WIDTH = 240

// One combined dropdown per column header: sort direction, a freeze/pin toggle, hiding
// the column, and the Excel-style value checklist filter, all behind a single click on
// the header itself. Hiding is one-directional from here - once hidden, this column's
// own header (and so this menu) disappears, so unhiding lives in ReportGrid's separate
// always-visible "Columns" toolbar button instead.
export function ColumnHeaderMenu({
  label,
  sortDir,
  frozen,
  values,
  selected,
  onSortAsc,
  onSortDesc,
  onToggleFrozen,
  onHide,
  onFilterChange,
}: ColumnHeaderMenuProps) {
  const { open, position, anchorRef, dropdownRef, toggleMenu, closeMenu } = useAnchoredDropdown(DROPDOWN_WIDTH)
  const [search, setSearch] = useState('')
  // Checkbox state staged in this dropdown until "Filter" is clicked - the grid itself
  // (via `selected`) only ever sees the committed value. Reset each time the menu opens:
  // starts blank (no entries checked) when there's no active filter yet, per the
  // "default no entry selected" behavior, or mirrors the already-committed set so
  // reopening a filtered column shows what's actually applied.
  const [pendingSelected, setPendingSelected] = useState<Set<string>>(() => new Set(selected ?? []))
  // Read inside the effect below via a ref, not a `selected` dependency - the effect
  // should only reset pendingSelected when the menu itself opens, not on every commit
  // that changes `selected` (e.g. right after this same dropdown's own Filter click).
  const selectedRef = useRef(selected)
  selectedRef.current = selected

  useEffect(() => {
    if (open) {
      setPendingSelected(new Set(selectedRef.current ?? []))
    } else {
      // Search box should start blank each time the menu reopens rather than remembering
      // the last query, since a stale filter over a fresh list of values is more
      // confusing than a momentary reset.
      setSearch('')
    }
  }, [open])

  const isActive = selected !== null
  const isChecked = (value: string) => pendingSelected.has(value)
  const visibleValues = search.trim()
    ? values.filter((value) => value.toLowerCase().includes(search.trim().toLowerCase()))
    : values

  const toggleValue = (value: string) => {
    setPendingSelected((prev) => {
      const next = new Set(prev)
      if (next.has(value)) {
        next.delete(value)
      } else {
        next.add(value)
      }
      return next
    })
  }

  const applyFilter = () => {
    // Every value checked is equivalent to no filter - otherwise a "fully selected"
    // explicit set and the true no-filter state would both be reachable, silently
    // disagreeing about whether the header should render its filter dot as active.
    onFilterChange(pendingSelected.size === values.length ? null : new Set(pendingSelected))
    closeMenu()
  }

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        className="report-th-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={toggleMenu}
      >
        <span className="report-th-label">{label}</span>
        <span className="report-th-indicators">
          {sortDir && (
            <span className="report-sort-indicator report-sort-indicator-active">
              {sortDir === 'asc' ? '▲' : '▼'}
            </span>
          )}
          {frozen && (
            <span className="report-th-dot report-th-dot-frozen" title="Frozen" aria-hidden="true" />
          )}
          {isActive && (
            <span className="report-th-dot report-th-dot-filter" title="Filtered" aria-hidden="true" />
          )}
        </span>
      </button>
      {open &&
        position &&
        createPortal(
          <div
            ref={dropdownRef}
            className="report-menu-dropdown"
            role="menu"
            style={{ position: 'fixed', top: position.top, left: position.left, width: DROPDOWN_WIDTH }}
          >
            <button
              type="button"
              className="report-menu-item"
              onClick={() => {
                onSortAsc()
                closeMenu()
              }}
            >
              Sort Asc
              {sortDir === 'asc' && <span aria-hidden="true">✓</span>}
            </button>
            <button
              type="button"
              className="report-menu-item"
              onClick={() => {
                onSortDesc()
                closeMenu()
              }}
            >
              Sort Desc
              {sortDir === 'desc' && <span aria-hidden="true">✓</span>}
            </button>

            <div className="report-menu-divider" />

            <label className="report-menu-checkbox">
              <input type="checkbox" checked={frozen} onChange={onToggleFrozen} />
              Freeze column
            </label>

            <div className="report-menu-divider" />

            <button
              type="button"
              className="report-menu-item"
              onClick={() => {
                onHide()
                closeMenu()
              }}
            >
              Hide column
            </button>

            <div className="report-menu-divider" />

            <div className="report-menu-section-title">Filter values</div>
            <div className="report-filter-search-wrap">
              <input
                type="text"
                className="report-filter-search"
                placeholder="Search values..."
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                onClick={(event) => event.stopPropagation()}
              />
            </div>
            <div className="report-filter-actions">
              <button
                type="button"
                className="report-filter-action"
                onClick={() => setPendingSelected(new Set(values))}
              >
                Select all
              </button>
              <button type="button" className="report-filter-action" onClick={() => setPendingSelected(new Set())}>
                Clear
              </button>
            </div>
            <ul className="report-filter-list">
              {visibleValues.map((value) => (
                <li key={value}>
                  <label className="report-filter-option">
                    <input type="checkbox" checked={isChecked(value)} onChange={() => toggleValue(value)} />
                    <span>{value}</span>
                  </label>
                </li>
              ))}
              {values.length === 0 && <li className="report-filter-empty">No values</li>}
              {values.length > 0 && visibleValues.length === 0 && (
                <li className="report-filter-empty">No values match "{search}"</li>
              )}
            </ul>
            <div className="report-filter-apply-wrap">
              <button type="button" className="job-button job-button-primary report-filter-apply" onClick={applyFilter}>
                Filter
              </button>
            </div>
          </div>,
          document.body,
        )}
    </>
  )
}
