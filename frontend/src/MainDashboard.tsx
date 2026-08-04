import { useCallback, useEffect, useMemo, useState } from 'react'
import type { AccountSummary, BlocklistedSymbol, PaginatedResearchResults, ResearchScheduleState, ResearchStatus, SystemEvent } from './api'
import { api } from './api'
import { AccountsTable } from './components/AccountsTable'
import { BlocklistPanel } from './components/BlocklistPanel'
import { NotificationsPanel } from './components/NotificationsPanel'
import { ResearchPanel } from './components/ResearchPanel'

const POLL_INTERVAL_MS = 15_000
const RESEARCH_POLL_INTERVAL_MS = 2_000
const DEFAULT_RESEARCH_PAGE_SIZE = 30

const EMPTY_RESEARCH_PAGE: PaginatedResearchResults = { items: [], total: 0, selected_total: 0, page: 1, page_size: DEFAULT_RESEARCH_PAGE_SIZE }

export function MainDashboard({ connected, onConnectedChange }: { connected: boolean; onConnectedChange: (connected: boolean) => void }) {
  const [accounts, setAccounts] = useState<AccountSummary[]>([])
  const [events, setEvents] = useState<SystemEvent[]>([])
  const [research, setResearch] = useState<PaginatedResearchResults>(EMPTY_RESEARCH_PAGE)
  const [researchPage, setResearchPage] = useState(1)
  const [researchPageSize, setResearchPageSize] = useState(DEFAULT_RESEARCH_PAGE_SIZE)
  const [researchSchedule, setResearchSchedule] = useState<ResearchScheduleState | null>(null)
  const [researchStatus, setResearchStatus] = useState<ResearchStatus | null>(null)
  const [blocklist, setBlocklist] = useState<BlocklistedSymbol[]>([])
  const [universe, setUniverse] = useState<string[]>([])

  useEffect(() => {
    api.researchUniverse().then(setUniverse)
  }, [])

  const refresh = useCallback(async () => {
    try {
      await api.health()
      onConnectedChange(true)
      const [accountsData, eventsData, researchData, researchScheduleData, researchStatusData, blocklistData] = await Promise.all([
        api.accounts(),
        api.events(),
        api.research(researchPage, researchPageSize),
        api.researchSchedule(),
        api.researchStatus(),
        api.blocklist(),
      ])
      setAccounts(accountsData)
      setEvents(eventsData)
      setResearch(researchData)
      setResearchSchedule(researchScheduleData)
      setResearchStatus(researchStatusData)
      setBlocklist(blocklistData)
    } catch {
      onConnectedChange(false)
    }
  }, [onConnectedChange, researchPage, researchPageSize])

  const refreshBlocklist = useCallback(async () => {
    setBlocklist(await api.blocklist())
  }, [])

  const blocklistedSymbols = useMemo(() => new Set(blocklist.map((b) => b.symbol)), [blocklist])

  const refreshResearch = useCallback(async () => {
    const [researchData, researchStatusData] = await Promise.all([api.research(researchPage, researchPageSize), api.researchStatus()])
    setResearch(researchData)
    setResearchStatus(researchStatusData)
  }, [researchPage, researchPageSize])

  const changeResearchPageSize = useCallback((size: number) => {
    setResearchPageSize(size)
    setResearchPage(1)
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
          research={research}
          schedule={researchSchedule}
          status={researchStatus}
          blocklist={blocklistedSymbols}
          universeSize={universe.length}
          onScheduleChange={setResearchSchedule}
          onTriggered={refreshResearch}
          onBlocklistChange={refreshBlocklist}
          onPageChange={setResearchPage}
          onPageSizeChange={changeResearchPageSize}
        />
      </div>

      <div className="card">
        <h2>Blocklist</h2>
        <BlocklistPanel items={blocklist} universe={universe} onChange={refreshBlocklist} />
      </div>

      <div className="card">
        <h2>Notifications</h2>
        <NotificationsPanel events={events} accounts={accounts} onChange={refresh} />
      </div>
    </main>
  )
}
