import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

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
  onFilterChange: (next: Set<string> | null) => void
}

const DROPDOWN_WIDTH = 240

// One combined dropdown per column header: sort direction, a freeze/pin toggle, and
// the Excel-style value checklist filter, all behind a single click on the header
// itself. Rendered through a portal into document.body, positioned via
// getBoundingClientRect from the trigger button rather than as an absolutely-
// positioned descendant of the th - the table lives inside .report-table-wrap's
// `overflow: auto`, which would otherwise clip the dropdown for any column near the
// scrolled-out edge.
export function ColumnHeaderMenu({
  label,
  sortDir,
  frozen,
  values,
  selected,
  onSortAsc,
  onSortDesc,
  onToggleFrozen,
  onFilterChange,
}: ColumnHeaderMenuProps) {
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const dropdownRef = useRef<HTMLDivElement>(null)

  const openMenu = () => {
    const rect = buttonRef.current?.getBoundingClientRect()
    if (rect) {
      // Right-aligned under the button when there's room; clamped to the viewport
      // otherwise so a column near either edge doesn't run off-screen.
      const left = Math.max(4, Math.min(rect.right - DROPDOWN_WIDTH, window.innerWidth - DROPDOWN_WIDTH - 4))
      setPosition({ top: rect.bottom + 4, left })
    }
    setOpen(true)
  }

  const closeMenu = () => setOpen(false)

  useEffect(() => {
    if (!open) return
    const close = (event: Event) => {
      const target = event.target as Node
      if (buttonRef.current?.contains(target) || dropdownRef.current?.contains(target)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', close)
    // The position above is captured once, on open - rather than tracking it live,
    // scrolling the table (the capture-phase listener catches that even though native
    // scroll events don't bubble) just closes the menu.
    document.addEventListener('scroll', close, true)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('scroll', close, true)
    }
  }, [open])

  const isActive = selected !== null
  const isChecked = (value: string) => selected === null || selected.has(value)

  const toggleValue = (value: string) => {
    const current = selected ?? new Set(values)
    const next = new Set(current)
    if (next.has(value)) {
      next.delete(value)
    } else {
      next.add(value)
    }
    // Re-checking back to every value collapses to null - otherwise a "fully selected"
    // explicit set and the true no-filter state would both be reachable, silently
    // disagreeing about whether the header should render its filter dot as active.
    onFilterChange(next.size === values.length ? null : next)
  }

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className="report-th-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => (open ? closeMenu() : openMenu())}
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

            <div className="report-menu-section-title">Filter values</div>
            <div className="report-filter-actions">
              <button type="button" className="report-filter-action" onClick={() => onFilterChange(null)}>
                Select all
              </button>
              <button type="button" className="report-filter-action" onClick={() => onFilterChange(new Set())}>
                Clear
              </button>
            </div>
            <ul className="report-filter-list">
              {values.map((value) => (
                <li key={value}>
                  <label className="report-filter-option">
                    <input type="checkbox" checked={isChecked(value)} onChange={() => toggleValue(value)} />
                    <span>{value}</span>
                  </label>
                </li>
              ))}
              {values.length === 0 && <li className="report-filter-empty">No values</li>}
            </ul>
          </div>,
          document.body,
        )}
    </>
  )
}
