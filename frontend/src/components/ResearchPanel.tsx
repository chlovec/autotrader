import { useState } from 'react'
import type { ResearchResult, ResearchScheduleState, ResearchStatus } from '../api'
import { api } from '../api'
import { ResearchDetailModal } from './ResearchDetailModal'

function formatRelativeTime(iso: string): string {
  const diffMinutes = Math.round((Date.now() - new Date(iso).getTime()) / 60_000)
  if (diffMinutes < 1) return 'just now'
  if (diffMinutes < 60) return `${diffMinutes}m ago`
  const diffHours = Math.round(diffMinutes / 60)
  if (diffHours < 24) return `${diffHours}h ago`
  return `${Math.round(diffHours / 24)}d ago`
}

export function ResearchPanel({
  results,
  schedule,
  status,
  onScheduleChange,
  onTriggered,
}: {
  results: ResearchResult[]
  schedule: ResearchScheduleState | null
  status: ResearchStatus | null
  onScheduleChange: (next: ResearchScheduleState) => void
  onTriggered: () => void
}) {
  const [toggleBusy, setToggleBusy] = useState(false)
  const [triggering, setTriggering] = useState(false)
  const [selected, setSelected] = useState<ResearchResult | null>(null)

  const running = triggering || Boolean(status?.running)

  async function toggleSchedule() {
    if (!schedule) return
    setToggleBusy(true)
    try {
      onScheduleChange(await api.setResearchSchedule(!schedule.enabled))
    } finally {
      setToggleBusy(false)
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

  const lastRunAt = results[0]?.run_at
  const selectedCount = results.filter((r) => r.selected).length

  return (
    <>
      <div className="research-header">
        <label className="research-toggle">
          <input type="checkbox" checked={schedule?.enabled ?? false} disabled={!schedule || toggleBusy} onChange={toggleSchedule} />
          Run nightly
        </label>
        <button type="button" className="btn resume" onClick={runNow} disabled={running}>
          {running ? 'Running…' : 'Run research now'}
        </button>
      </div>

      {lastRunAt ? (
        <p className="empty-note">
          Last run {formatRelativeTime(lastRunAt)} · {results.length} scored · {selectedCount} selected for the watchlist
        </p>
      ) : (
        <p className="empty-note">No research runs yet — click "Run research now" or wait for the nightly run.</p>
      )}

      {results.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Combined</th>
              <th>Technical</th>
              <th>News</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {results.map((r) => (
              <tr
                key={r.symbol}
                className="clickable-row"
                role="button"
                tabIndex={0}
                onClick={() => setSelected(r)}
                onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && setSelected(r)}
              >
                <td className="emphasis">{r.symbol}</td>
                <td>{r.combined_score.toFixed(1)}</td>
                <td>{r.technical_score.toFixed(1)}</td>
                <td>{r.news_score.toFixed(1)}</td>
                <td>{r.selected && <span className="badge badge-selected">selected</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selected && <ResearchDetailModal result={selected} onClose={() => setSelected(null)} />}
    </>
  )
}
