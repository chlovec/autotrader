import { useEffect, useState } from 'react'
import type { PaginatedResearchResults, ResearchResult, ResearchScheduleState, ResearchStatus } from '../api'
import { api } from '../api'
import { ResearchDetailModal } from './ResearchDetailModal'

const PAGE_SIZE_OPTIONS = [10, 25, 30, 50, 100, 200]

function formatRelativeTime(iso: string): string {
  const diffMinutes = Math.round((Date.now() - new Date(iso).getTime()) / 60_000)
  if (diffMinutes < 1) return 'just now'
  if (diffMinutes < 60) return `${diffMinutes}m ago`
  const diffHours = Math.round(diffMinutes / 60)
  if (diffHours < 24) return `${diffHours}h ago`
  return `${Math.round(diffHours / 24)}d ago`
}

export function ResearchPanel({
  research,
  schedule,
  status,
  blocklist,
  universeSize,
  onScheduleChange,
  onTriggered,
  onBlocklistChange,
  onPageChange,
  onPageSizeChange,
}: {
  research: PaginatedResearchResults
  schedule: ResearchScheduleState | null
  status: ResearchStatus | null
  blocklist: Set<string>
  universeSize: number
  onScheduleChange: (next: ResearchScheduleState) => void
  onTriggered: () => void
  onBlocklistChange: () => void
  onPageChange: (page: number) => void
  onPageSizeChange: (size: number) => void
}) {
  const [toggleBusy, setToggleBusy] = useState(false)
  const [triggering, setTriggering] = useState(false)
  const [selected, setSelected] = useState<ResearchResult | null>(null)
  const [blockBusy, setBlockBusy] = useState<string | null>(null)
  const [selectedCountInput, setSelectedCountInput] = useState(String(schedule?.selected_count ?? ''))
  const [selectedCountBusy, setSelectedCountBusy] = useState(false)
  const [selectedCountSaved, setSelectedCountSaved] = useState(false)
  const [pageInput, setPageInput] = useState(String(research.page))

  useEffect(() => {
    setSelectedCountInput(String(schedule?.selected_count ?? ''))
  }, [schedule?.selected_count])

  useEffect(() => {
    setPageInput(String(research.page))
  }, [research.page])

  const running = triggering || Boolean(status?.running)
  const selectedCountDirty = schedule != null && Number(selectedCountInput) !== schedule.selected_count

  async function toggleBlock(symbol: string, isBlocked: boolean) {
    setBlockBusy(symbol)
    try {
      if (isBlocked) await api.removeFromBlocklist(symbol)
      else await api.addToBlocklist(symbol)
      onBlocklistChange()
    } finally {
      setBlockBusy(null)
    }
  }

  async function toggleSchedule() {
    if (!schedule) return
    setToggleBusy(true)
    try {
      onScheduleChange(await api.setResearchSchedule({ ...schedule, enabled: !schedule.enabled }))
    } finally {
      setToggleBusy(false)
    }
  }

  async function saveSelectedCount() {
    if (!schedule) return
    setSelectedCountBusy(true)
    try {
      onScheduleChange(await api.setResearchSchedule({ ...schedule, selected_count: Number(selectedCountInput) }))
      setSelectedCountSaved(true)
      setTimeout(() => setSelectedCountSaved(false), 2000)
    } finally {
      setSelectedCountBusy(false)
    }
  }

  async function runNow() {
    setTriggering(true)
    try {
      await api.triggerResearch()
      onTriggered()
    } finally {
      setTriggering(false)
    }
  }

  const { items, total, selected_total: selectedTotal, page, page_size: pageSize } = research
  const lastRunAt = items[0]?.run_at
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const progress = running && universeSize > 0 ? ` of ${universeSize.toLocaleString()} (${Math.round((total / universeSize) * 100)}%)` : ''

  function commitPageInput() {
    const parsed = Math.round(Number(pageInput))
    const clamped = Number.isFinite(parsed) ? Math.min(Math.max(parsed, 1), totalPages) : page
    setPageInput(String(clamped))
    if (clamped !== page) onPageChange(clamped)
  }

  return (
    <>
      <div className="research-header">
        <label className="research-toggle">
          <input type="checkbox" checked={schedule?.enabled ?? false} disabled={!schedule || toggleBusy} onChange={toggleSchedule} />
          Run nightly
        </label>
        <label className="research-selected-count">
          Selected count
          <input
            type="number"
            min="1"
            step="1"
            value={selectedCountInput}
            disabled={!schedule || selectedCountBusy}
            onChange={(e) => setSelectedCountInput(e.target.value)}
          />
        </label>
        <button type="button" className="btn-small" disabled={!selectedCountDirty || selectedCountBusy} onClick={saveSelectedCount}>
          {selectedCountBusy ? 'Saving…' : selectedCountSaved ? 'Saved' : 'Save'}
        </button>
        <button type="button" className="btn resume" onClick={runNow} disabled={running}>
          {running ? 'Running…' : 'Run research now'}
        </button>
      </div>

      {lastRunAt ? (
        <p className="empty-note">
          Last run {formatRelativeTime(lastRunAt)} · {total.toLocaleString()}
          {progress} scored · {selectedTotal.toLocaleString()} selected for the watchlist
        </p>
      ) : (
        <p className="empty-note">No research runs yet — click "Run research now" or wait for the nightly run.</p>
      )}

      {items.length > 0 && (
        <>
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Combined</th>
                <th>Technical</th>
                <th>News</th>
                <th />
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((r) => {
                const isBlocked = blocklist.has(r.symbol)
                return (
                  <tr
                    key={r.symbol}
                    className={`clickable-row${isBlocked ? ' blocklisted' : ''}`}
                    role="button"
                    tabIndex={0}
                    onClick={() => setSelected(r)}
                    onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && setSelected(r)}
                  >
                    <td className="emphasis">{r.symbol}</td>
                    <td>{r.combined_score.toFixed(1)}</td>
                    <td>{r.technical_score.toFixed(1)}</td>
                    <td>{r.news_score.toFixed(1)}</td>
                    <td>
                      {isBlocked ? (
                        <span className="badge badge-blocklisted">blocklisted</span>
                      ) : (
                        r.selected && <span className="badge badge-selected">selected</span>
                      )}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="btn-small"
                        disabled={blockBusy === r.symbol}
                        onClick={(e) => {
                          e.stopPropagation()
                          toggleBlock(r.symbol, isBlocked)
                        }}
                      >
                        {isBlocked ? 'Unblock' : 'Block'}
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>

          <div className="research-pagination">
            <label className="research-page-size">
              Per page
              <select value={pageSize} onChange={(e) => onPageSizeChange(Number(e.target.value))}>
                {PAGE_SIZE_OPTIONS.map((size) => (
                  <option key={size} value={size}>
                    {size}
                  </option>
                ))}
              </select>
            </label>
            <div className="research-pager">
              <button type="button" className="btn-small" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>
                Prev
              </button>
              <span className="research-page-jump">
                Page{' '}
                <input
                  type="number"
                  min="1"
                  max={totalPages}
                  value={pageInput}
                  onChange={(e) => setPageInput(e.target.value)}
                  onBlur={commitPageInput}
                  onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
                />{' '}
                of {totalPages}
              </span>
              <button type="button" className="btn-small" disabled={page >= totalPages} onClick={() => onPageChange(page + 1)}>
                Next
              </button>
            </div>
          </div>
        </>
      )}

      {selected && (
        <ResearchDetailModal result={selected} blocklisted={blocklist.has(selected.symbol)} onClose={() => setSelected(null)} />
      )}
    </>
  )
}
