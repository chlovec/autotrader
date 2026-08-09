import { createPortal } from 'react-dom'
import { useEffect, useRef, useState } from 'react'
import { NUMERIC_FILTER_OPS, type NumericCondition, type NumericFilterOp } from '../numericFilter'
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
  // Whether the column's underlying values are numbers (see ReportGrid's numericKeys) -
  // gates whether the </<=/>/>= condition filter section below renders at all. Value-set
  // filtering above still applies to numeric columns too; conditions are additive.
  numeric: boolean
  // Active conditions (ANDed together) for this column, or null/empty for none.
  numericConditions: NumericCondition[] | null
  onSortAsc: () => void
  onSortDesc: () => void
  onToggleFrozen: () => void
  onHide: () => void
  onFilterChange: (next: Set<string> | null) => void
  onNumericFilterChange: (next: NumericCondition[] | null) => void
}

const DROPDOWN_WIDTH = 240
// Two rows (op + value) shown at once - covers the common "between" case (e.g. >= 10
// AND <= 50) without an open-ended "add condition" control few reports would ever need
// more than.
const NUMERIC_CONDITION_SLOTS = 2

type ConditionSlot = { op: NumericFilterOp; value: string }

function emptySlot(): ConditionSlot {
  return { op: '>=', value: '' }
}

function slotsFromConditions(conditions: NumericCondition[] | null): ConditionSlot[] {
  const slots = (conditions ?? []).map((c) => ({ op: c.op, value: String(c.value) }))
  while (slots.length < NUMERIC_CONDITION_SLOTS) slots.push(emptySlot())
  return slots.slice(0, NUMERIC_CONDITION_SLOTS)
}

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
  numeric,
  numericConditions,
  onSortAsc,
  onSortDesc,
  onToggleFrozen,
  onHide,
  onFilterChange,
  onNumericFilterChange,
}: ColumnHeaderMenuProps) {
  const { open, position, anchorRef, dropdownRef, toggleMenu, closeMenu } = useAnchoredDropdown(DROPDOWN_WIDTH)
  const [search, setSearch] = useState('')
  // Checkbox state staged in this dropdown until "Filter" is clicked - the grid itself
  // (via `selected`) only ever sees the committed value. Reset each time the menu opens:
  // starts blank (no entries checked) when there's no active filter yet, per the
  // "default no entry selected" behavior, or mirrors the already-committed set so
  // reopening a filtered column shows what's actually applied.
  const [pendingSelected, setPendingSelected] = useState<Set<string>>(() => new Set(selected ?? []))
  // Same staging pattern as pendingSelected, for the </<=/>/>= condition rows.
  const [pendingConditions, setPendingConditions] = useState<ConditionSlot[]>(() => slotsFromConditions(numericConditions))
  // Read inside the effect below via a ref, not a `selected`/`numericConditions`
  // dependency - the effect should only reset staged state when the menu itself opens,
  // not on every commit that changes them (e.g. right after this same dropdown's own
  // Filter click).
  const selectedRef = useRef(selected)
  selectedRef.current = selected
  const numericConditionsRef = useRef(numericConditions)
  numericConditionsRef.current = numericConditions

  useEffect(() => {
    if (open) {
      setPendingSelected(new Set(selectedRef.current ?? []))
      setPendingConditions(slotsFromConditions(numericConditionsRef.current))
    } else {
      // Search box should start blank each time the menu reopens rather than remembering
      // the last query, since a stale filter over a fresh list of values is more
      // confusing than a momentary reset.
      setSearch('')
    }
  }, [open])

  const isActive = selected !== null || (numericConditions?.length ?? 0) > 0
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

  const setSlotOp = (index: number, op: NumericFilterOp) => {
    setPendingConditions((prev) => prev.map((slot, i) => (i === index ? { ...slot, op } : slot)))
  }
  const setSlotValue = (index: number, value: string) => {
    setPendingConditions((prev) => prev.map((slot, i) => (i === index ? { ...slot, value } : slot)))
  }

  const applyNumericFilter = () => {
    const conditions: NumericCondition[] = pendingConditions
      .filter((slot) => slot.value.trim() !== '' && Number.isFinite(Number(slot.value)))
      .map((slot) => ({ op: slot.op, value: Number(slot.value) }))
    onNumericFilterChange(conditions.length > 0 ? conditions : null)
    closeMenu()
  }

  const clearNumericFilter = () => {
    setPendingConditions(slotsFromConditions(null))
    onNumericFilterChange(null)
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

            {numeric && (
              <>
                <div className="report-menu-divider" />
                <div className="report-menu-section-title">Filter by condition</div>
                <div className="report-numeric-filter">
                  {pendingConditions.map((slot, i) => (
                    <div className="report-numeric-filter-row" key={i}>
                      <select
                        className="report-numeric-filter-select"
                        value={slot.op}
                        onChange={(event) => setSlotOp(i, event.target.value as NumericFilterOp)}
                      >
                        {NUMERIC_FILTER_OPS.map((op) => (
                          <option key={op} value={op}>
                            {op}
                          </option>
                        ))}
                      </select>
                      <input
                        type="number"
                        className="report-numeric-filter-input"
                        placeholder="Value"
                        value={slot.value}
                        onChange={(event) => setSlotValue(i, event.target.value)}
                      />
                    </div>
                  ))}
                  <p className="report-numeric-filter-hint">Both conditions apply together (AND) when set.</p>
                </div>
                <div className="report-filter-actions report-numeric-filter-actions">
                  <button type="button" className="report-filter-action" onClick={clearNumericFilter}>
                    Clear
                  </button>
                </div>
                <div className="report-filter-apply-wrap">
                  <button
                    type="button"
                    className="job-button job-button-primary report-filter-apply"
                    onClick={applyNumericFilter}
                  >
                    Apply condition
                  </button>
                </div>
              </>
            )}

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
