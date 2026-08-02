import { useCallback, useEffect, useState } from 'react'
import type { AccountSummary, ResearchResult, ResearchScheduleState, ResearchStatus, SystemEvent } from './api'
import { api } from './api'
import { AccountsTable } from './components/AccountsTable'
import { NotificationsPanel } from './components/NotificationsPanel'
import { ResearchPanel } from './components/ResearchPanel'

const POLL_INTERVAL_MS = 15_000
const RESEARCH_POLL_INTERVAL_MS = 2_000

export function MainDashboard({ connected, onConnectedChange }: { connected: boolean; onConnectedChange: (connected: boolean) => void }) {
  const [accounts, setAccounts] = useState<AccountSummary[]>([])
  const [events, setEvents] = useState<SystemEvent[]>([])
  const [research, setResearch] = useState<ResearchResult[]>([])
  const [researchSchedule, setResearchSchedule] = useState<ResearchScheduleState | null>(null)
  const [researchStatus, setResearchStatus] = useState<ResearchStatus | null>(null)

  const refresh = useCallback(async () => {
    try {
      await api.health()
      onConnectedChange(true)
      const [accountsData, eventsData, researchData, researchScheduleData, researchStatusData] = await Promise.all([
        api.accounts(),
        api.events(),
        api.research(),
        api.researchSchedule(),
        api.researchStatus(),
      ])
      setAccounts(accountsData)
      setEvents(eventsData)
      setResearch(researchData)
      setResearchSchedule(researchScheduleData)
      setResearchStatus(researchStatusData)
    } catch {
      onConnectedChange(false)
    }
  }, [onConnectedChange])

  const refreshResearch = useCallback(async () => {
    const [researchData, researchStatusData] = await Promise.all([api.research(), api.researchStatus()])
    setResearch(researchData)
    setResearchStatus(researchStatusData)
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [refresh])

  useEffect(() => {
    if (!researchStatus?.running) return
    const interval = setInterval(refreshResearch, RESEARCH_POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [researchStatus?.running, refreshResearch])

  return (
    <main className="dashboard">
      <div className="dashboard-header">
        <h1>Autotrader Dashboard</h1>
        <span className="connection-status">
          <span className={`connection-dot ${connected ? '' : 'offline'}`} />
          {connected ? 'Connected' : 'Backend unreachable'}
        </span>
      </div>

      <div className="card">
        <h2>Accounts</h2>
        <AccountsTable accounts={accounts} onChange={refresh} />
      </div>

      <div className="card">
        <h2>Research</h2>
        <ResearchPanel
          results={research}
          schedule={researchSchedule}
          status={researchStatus}
          onScheduleChange={setResearchSchedule}
          onTriggered={refreshResearch}
        />
      </div>

      <div className="card">
        <h2>Notifications</h2>
        <NotificationsPanel events={events} accounts={accounts} onChange={refresh} />
      </div>
    </main>
  )
}
