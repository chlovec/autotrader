import { useEffect, useState } from 'react'
import { api, type WatchlistRow } from '../api'
import { SearchableSelect } from './SearchableSelect'
import { DragHandleIcon } from './icons'

function formatAddedAt(value: string): string {
  // Stored naive-but-UTC by the backend (see db/models.py's TickerGroup.created_at).
  return new Date(`${value}Z`).toLocaleString()
}

export function WatchlistPage() {
  const [rows, setRows] = useState<WatchlistRow[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<string[]>([])
  const [adding, setAdding] = useState(false)
  const [removing, setRemoving] = useState<Set<string>>(new Set())
  const [reordering, setReordering] = useState(false)
  const [dragged, setDragged] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState<string | null>(null)

  const load = () => {
    setLoading(true)
    setError(null)
    api
      .watchlist()
      .then(setRows)
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load watchlist'))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  const watched = new Set(rows?.map((row) => row.ticker) ?? [])

  // Adds each selected ticker in turn rather than failing the whole batch on the first
  // error - any that didn't make it stay selected so they can be retried.
  const add = async () => {
    setAdding(true)
    setError(null)
    const added: WatchlistRow[] = []
    const failed: string[] = []
    const messages: string[] = []
    for (const ticker of pending) {
      try {
        added.push(await api.addToWatchlist(ticker))
      } catch (err) {
        failed.push(ticker)
        messages.push(err instanceof Error ? err.message : `Failed to add ${ticker}`)
      }
    }
    setRows((prev) => [...added.reverse(), ...(prev ?? [])])
    setPending(failed)
    if (messages.length) setError(messages.join('; '))
    setAdding(false)
  }

  // Applies the new order optimistically, then replaces it with the server's copy - or
  // reloads on failure (e.g. a 409 because another tab changed the watchlist).
  const move = async (fromIndex: number, toIndex: number) => {
    if (!rows || fromIndex === toIndex || toIndex < 0 || toIndex >= rows.length) return
    const next = [...rows]
    const [moved] = next.splice(fromIndex, 1)
    next.splice(toIndex, 0, moved)
    setRows(next)
    setReordering(true)
    setError(null)
    try {
      setRows(await api.reorderWatchlist(next.map((row) => row.ticker)))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reorder watchlist')
      load()
    } finally {
      setReordering(false)
    }
  }

  const handleDragEnd = () => {
    setDragged(null)
    setDragOver(null)
  }

  const handleDrop = (target: string) => {
    const source = dragged
    handleDragEnd()
    if (!rows || !source) return
    move(
      rows.findIndex((row) => row.ticker === source),
      rows.findIndex((row) => row.ticker === target),
    )
  }

  const remove = async (ticker: string) => {
    setRemoving((prev) => new Set(prev).add(ticker))
    setError(null)
    try {
      await api.removeFromWatchlist(ticker)
      setRows((prev) => prev?.filter((row) => row.ticker !== ticker) ?? null)
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to remove ${ticker}`)
    } finally {
      setRemoving((prev) => {
        const next = new Set(prev)
        next.delete(ticker)
        return next
      })
    }
  }

  return (
    <div className="report-page">
      <h1 className="jobs-page-title">Watchlist</h1>
      <p className="jobs-page-subtitle">
        Tickers you want to monitor. Search to add one or more, drag or use the arrows to reorder, or remove any below.
      </p>

      <div className="report-controls">
        <div className="report-controls-fields">
          <div className="job-field report-ticker-type-field">
            <span className="job-field-label">Add tickers</span>
            <SearchableSelect
              multiple
              selected={pending}
              onChange={setPending}
              disabled={adding}
              onSearch={(q) =>
                api.searchTickers(q).then((matches) =>
                  matches
                    .filter((t) => !watched.has(t.ticker))
                    .map((t) => ({ value: t.ticker, label: t.name ? `${t.ticker} — ${t.name}` : t.ticker })),
                )
              }
              placeholder="Search tickers..."
            />
          </div>
        </div>
        <div className="report-controls-actions">
          <button
            type="button"
            className="job-button job-button-primary"
            disabled={pending.length === 0 || adding}
            onClick={add}
          >
            {adding ? 'Adding...' : pending.length > 1 ? `Add ${pending.length} tickers` : 'Add to watchlist'}
          </button>
        </div>
      </div>

      {error && <p className="jobs-error">{error}</p>}

      {loading && <p className="placeholder-note">Loading watchlist...</p>}

      {!loading && rows && rows.length === 0 && (
        <p className="placeholder-note">Your watchlist is empty. Search for a ticker above to start monitoring it.</p>
      )}

      {!loading && rows && rows.length > 0 && (
        <div className="report-grid">
          <div className="report-table-wrap">
            <table className="job-history-table report-table">
              <thead>
                <tr>
                  <th aria-label="Order"></th>
                  <th>Ticker</th>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Exchange</th>
                  <th>Added</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => {
                  const isRemoving = removing.has(row.ticker)
                  const rowClass = [
                    dragged === row.ticker ? 'watchlist-row-dragging' : '',
                    dragOver === row.ticker ? 'watchlist-row-drag-over' : '',
                  ]
                    .filter(Boolean)
                    .join(' ')
                  return (
                    <tr
                      key={row.ticker}
                      className={rowClass || undefined}
                      onDragOver={(event) => event.preventDefault()}
                      onDragEnter={() => dragged && dragged !== row.ticker && setDragOver(row.ticker)}
                      onDrop={(event) => {
                        event.preventDefault()
                        handleDrop(row.ticker)
                      }}
                    >
                      <td>
                        <div className="watchlist-order-cell">
                          <button
                            type="button"
                            className="icon-button job-drag-handle"
                            aria-label={`Drag to reorder ${row.ticker}`}
                            draggable={!reordering}
                            onDragStart={() => setDragged(row.ticker)}
                            onDragEnd={handleDragEnd}
                          >
                            <DragHandleIcon className="icon" />
                          </button>
                          <button
                            type="button"
                            className="order-by-move"
                            disabled={index === 0 || reordering}
                            onClick={() => move(index, index - 1)}
                            title="Move up"
                            aria-label={`Move ${row.ticker} up`}
                          >
                            ↑
                          </button>
                          <button
                            type="button"
                            className="order-by-move"
                            disabled={index === rows.length - 1 || reordering}
                            onClick={() => move(index, index + 1)}
                            title="Move down"
                            aria-label={`Move ${row.ticker} down`}
                          >
                            ↓
                          </button>
                        </div>
                      </td>
                      <td>{row.ticker}</td>
                      <td>{row.name ?? '—'}</td>
                      <td>{row.type ?? '—'}</td>
                      <td>{row.primary_exchange ?? '—'}</td>
                      <td>{formatAddedAt(row.added_at)}</td>
                      <td>
                        <button
                          type="button"
                          className="job-button job-button-danger"
                          disabled={isRemoving}
                          onClick={() => remove(row.ticker)}
                        >
                          {isRemoving ? 'Removing...' : 'Remove'}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
