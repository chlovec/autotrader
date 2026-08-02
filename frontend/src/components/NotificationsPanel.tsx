import { useMemo } from 'react'
import type { AccountSummary, SystemEvent } from '../api'
import { api } from '../api'
import { EventsFeed } from './EventsFeed'

// Most severe first, so a noisy "General" section doesn't bury a critical account alert
// below a wall of info-level rows.
const LEVEL_ORDER: SystemEvent['level'][] = ['critical', 'error', 'warning', 'info']

interface NotificationGroup {
  accountId: string | null
  label: string
  events: SystemEvent[]
  byLevel: Partial<Record<SystemEvent['level'], SystemEvent[]>>
}

function groupEvents(events: SystemEvent[], accounts: AccountSummary[]): NotificationGroup[] {
  const nameById = new Map(accounts.map((a) => [a.id, a.display_name]))
  const byKey = new Map<string, NotificationGroup>()

  for (const event of events) {
    const key = event.account_id ?? '__general__'
    let group = byKey.get(key)
    if (!group) {
      group = {
        accountId: event.account_id,
        label: event.account_id ? (nameById.get(event.account_id) ?? event.account_id) : 'General',
        events: [],
        byLevel: {},
      }
      byKey.set(key, group)
    }
    group.events.push(event)
    ;(group.byLevel[event.level] ??= []).push(event)
  }

  // Account groups in the order accounts are listed on the dashboard, then any group for
  // an account not in that list, then General last.
  const ordered: NotificationGroup[] = []
  for (const account of accounts) {
    const group = byKey.get(account.id)
    if (group) ordered.push(group)
  }
  for (const [key, group] of byKey) {
    if (key !== '__general__' && !accounts.some((a) => a.id === group.accountId)) ordered.push(group)
  }
  const general = byKey.get('__general__')
  if (general) ordered.push(general)
  return ordered
}

export function NotificationsPanel({
  events,
  accounts,
  onChange,
}: {
  events: SystemEvent[]
  accounts: AccountSummary[]
  onChange: () => void
}) {
  const groups = useMemo(() => groupEvents(events, accounts), [events, accounts])

  const clearOne = async (id: number) => {
    await api.clearEvent(id)
    onChange()
  }

  const clearGroup = async (group: NotificationGroup) => {
    await api.clearEvents(group.accountId ? { accountId: group.accountId } : { unassigned: true })
    onChange()
  }

  const clearAll = async () => {
    await api.clearEvents()
    onChange()
  }

  if (events.length === 0) {
    return <p className="empty-note">No notifications — everything's been running quietly.</p>
  }

  return (
    <div className="notifications-panel">
      <div className="notifications-header">
        <span className="notifications-count">
          {events.length} notification{events.length === 1 ? '' : 's'}
        </span>
        <button type="button" className="btn-small" onClick={clearAll}>
          Clear all
        </button>
      </div>

      {groups.map((group) => (
        <div key={group.accountId ?? '__general__'} className="notification-group">
          <div className="notification-group-header">
            <h3>{group.label}</h3>
            <button type="button" className="btn-small" onClick={() => clearGroup(group)}>
              Clear
            </button>
          </div>
          {LEVEL_ORDER.filter((level) => group.byLevel[level]?.length).map((level) => (
            <div key={level} className="notification-type-group">
              <span className={`notification-type-label event-${level}`}>{level.toUpperCase()}</span>
              <EventsFeed events={group.byLevel[level]!} onClear={clearOne} />
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
