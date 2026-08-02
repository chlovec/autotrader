import type { SystemEvent } from '../api'

const LEVEL_ICON: Record<SystemEvent['level'], string> = {
  info: 'ℹ',
  warning: '▲',
  error: '✕',
  critical: '■',
}

export function EventsFeed({ events, onClear }: { events: SystemEvent[]; onClear?: (id: number) => void }) {
  if (events.length === 0) {
    return <p className="empty-note">No system events — everything's been running quietly.</p>
  }

  return (
    <ul className="events-feed">
      {events.map((e) => (
        <li key={e.id} className={`event-row event-${e.level}`}>
          <span className="event-badge">
            <span aria-hidden="true">{LEVEL_ICON[e.level]}</span> {e.level.toUpperCase()}
          </span>
          <span className="event-source">{e.source}</span>
          <span className="event-message">{e.message}</span>
          <span className="event-time">{new Date(e.timestamp).toLocaleString()}</span>
          {onClear && (
            <button type="button" className="event-clear" aria-label="Clear notification" onClick={() => onClear(e.id)}>
              ✕
            </button>
          )}
        </li>
      ))}
    </ul>
  )
}
