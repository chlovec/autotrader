import { useMemo, useRef, useState } from 'react'
import type { BlocklistedSymbol } from '../api'
import { api } from '../api'

const MAX_OPTIONS = 8

export function BlocklistPanel({
  items,
  universe,
  onChange,
}: {
  items: BlocklistedSymbol[]
  universe: string[]
  onChange: () => void
}) {
  const [query, setQuery] = useState('')
  // Only ever set by picking an option below, never by typing directly - that's what makes
  // "Add" only submit a symbol that was actually selected from the search, not free text.
  const [picked, setPicked] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [highlighted, setHighlighted] = useState(0)
  const [busy, setBusy] = useState(false)
  const closeTimeout = useRef<ReturnType<typeof setTimeout> | null>(null)

  const blocked = useMemo(() => new Set(items.map((i) => i.symbol)), [items])

  const options = useMemo(() => {
    const q = query.trim().toUpperCase()
    return universe.filter((sym) => !blocked.has(sym) && (q === '' || sym.includes(q))).slice(0, MAX_OPTIONS)
  }, [query, universe, blocked])

  function pick(sym: string) {
    setPicked(sym)
    setQuery(sym)
    setOpen(false)
  }

  function handleQueryChange(value: string) {
    setQuery(value)
    setPicked(null)
    setOpen(true)
    setHighlighted(0)
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!open || options.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlighted((h) => (h + 1) % options.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlighted((h) => (h - 1 + options.length) % options.length)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      pick(options[highlighted])
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  async function add(e: React.FormEvent) {
    e.preventDefault()
    if (!picked) return
    setBusy(true)
    try {
      await api.addToBlocklist(picked)
      setQuery('')
      setPicked(null)
      onChange()
    } finally {
      setBusy(false)
    }
  }

  async function remove(sym: string) {
    await api.removeFromBlocklist(sym)
    onChange()
  }

  return (
    <>
      <form className="blocklist-form" onSubmit={add}>
        <div className="combobox">
          <input
            type="text"
            role="combobox"
            aria-expanded={open}
            aria-autocomplete="list"
            placeholder="Search symbol (e.g. TSLA)"
            value={query}
            disabled={busy}
            onChange={(e) => handleQueryChange(e.target.value)}
            onFocus={() => setOpen(true)}
            onKeyDown={handleKeyDown}
            onBlur={() => {
              closeTimeout.current = setTimeout(() => setOpen(false), 100)
            }}
          />
          {open && options.length > 0 && (
            <ul className="combobox-options" role="listbox">
              {options.map((sym, i) => (
                <li
                  key={sym}
                  role="option"
                  aria-selected={i === highlighted}
                  className={i === highlighted ? 'active' : ''}
                  onMouseDown={(e) => {
                    // Fires before the input's blur, so the option is picked before the
                    // dropdown-closing blur handler would otherwise beat it to closing.
                    e.preventDefault()
                    if (closeTimeout.current) clearTimeout(closeTimeout.current)
                    pick(sym)
                  }}
                  onMouseEnter={() => setHighlighted(i)}
                >
                  {sym}
                </li>
              ))}
            </ul>
          )}
        </div>
        <button type="submit" className="btn-small" disabled={busy || !picked}>
          Add
        </button>
      </form>

      {items.length === 0 ? (
        <p className="empty-note">No symbols blocklisted.</p>
      ) : (
        <ul className="blocklist-items">
          {items.map((item) => (
            <li key={item.symbol} className="blocklist-item">
              <span className="badge badge-blocklisted">{item.symbol}</span>
              <button type="button" className="btn-small" onClick={() => remove(item.symbol)}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}
