import { useEffect, useRef, useState } from 'react'

export type SelectOption = { value: string; label: string }

type SearchableSelectProps = {
  selected: string[]
  onChange: (values: string[]) => void
  multiple: boolean
  disabled?: boolean
  placeholder: string
  // Static option list, filtered client-side as the user types (e.g. ticker types -
  // there are only ever a handful of distinct values).
  options?: SelectOption[]
  // Server-backed search instead of a static list (e.g. tickers, which can number in
  // the thousands) - debounced, and only called once the user has typed something so
  // the dropdown never has to ship the whole tickers table just to filter it.
  onSearch?: (query: string) => Promise<SelectOption[]>
}

const DEBOUNCE_MS = 250

export function SearchableSelect({
  selected,
  onChange,
  multiple,
  disabled,
  placeholder,
  options,
  onSearch,
}: SearchableSelectProps) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [results, setResults] = useState<SelectOption[]>(options ?? [])
  const [loading, setLoading] = useState(false)
  const debounceRef = useRef<number | null>(null)
  const blurTimeoutRef = useRef<number | null>(null)

  // Client-side filtering of a static option list.
  useEffect(() => {
    if (onSearch) return
    const term = query.trim().toLowerCase()
    const source = options ?? []
    setResults(term ? source.filter((o) => o.label.toLowerCase().includes(term)) : source)
  }, [query, options, onSearch])

  // Debounced server-side search.
  useEffect(() => {
    if (!onSearch) return
    if (debounceRef.current) window.clearTimeout(debounceRef.current)
    const term = query.trim()
    if (!term) {
      setResults([])
      return
    }
    setLoading(true)
    debounceRef.current = window.setTimeout(async () => {
      try {
        const next = await onSearch(term)
        setResults(next)
      } finally {
        setLoading(false)
      }
    }, DEBOUNCE_MS)
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
    }
  }, [query, onSearch])

  const labelOf = (value: string): string => {
    const match = options?.find((o) => o.value === value) ?? results.find((o) => o.value === value)
    return match ? match.label : value
  }

  const isSelected = (value: string) => selected.includes(value)

  // Options stay in the dropdown after being selected (styled via
  // combobox-option-selected) rather than disappearing - clicking a selected one
  // again toggles it off.
  const toggle = (value: string) => {
    if (multiple) {
      onChange(isSelected(value) ? selected.filter((v) => v !== value) : [...selected, value])
    } else {
      onChange([value])
      setOpen(false)
    }
    setQuery('')
  }

  const remove = (value: string) => {
    onChange(selected.filter((v) => v !== value))
  }

  const handleFocus = () => {
    if (blurTimeoutRef.current) window.clearTimeout(blurTimeoutRef.current)
    setOpen(true)
  }

  // Delayed so a click on a dropdown option (which doesn't itself steal focus, but a
  // click landing just outside the widget should still close it) has a chance to
  // register first.
  const handleBlur = () => {
    blurTimeoutRef.current = window.setTimeout(() => setOpen(false), 120)
  }

  const showInput = multiple || selected.length === 0

  return (
    <div className={`combobox${disabled ? ' combobox-disabled' : ''}`}>
      <div className="combobox-control">
        {selected.map((value) => (
          <span className="combobox-chip" key={value}>
            {labelOf(value)}
            {!disabled && (
              <button
                type="button"
                className="combobox-chip-remove"
                onClick={() => remove(value)}
                aria-label={`Remove ${labelOf(value)}`}
              >
                ×
              </button>
            )}
          </span>
        ))}
        {showInput && (
          <input
            type="text"
            className="combobox-input"
            value={query}
            disabled={disabled}
            placeholder={selected.length === 0 ? placeholder : ''}
            onChange={(e) => setQuery(e.target.value)}
            onFocus={handleFocus}
            onBlur={handleBlur}
          />
        )}
      </div>
      {open && !disabled && (
        <ul className="combobox-dropdown">
          {loading && <li className="combobox-empty">Searching...</li>}
          {!loading && onSearch && !query.trim() && <li className="combobox-empty">Type to search...</li>}
          {!loading && (query.trim() || !onSearch) && results.length === 0 && (
            <li className="combobox-empty">No matches</li>
          )}
          {!loading &&
            results.map((opt) => (
              <li
                key={opt.value}
                className={`combobox-option${isSelected(opt.value) ? ' combobox-option-selected' : ''}`}
                onClick={() => toggle(opt.value)}
              >
                {opt.label}
              </li>
            ))}
        </ul>
      )}
    </div>
  )
}
