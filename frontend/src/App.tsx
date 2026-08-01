import { useCallback, useEffect, useState } from 'react'
import { API_BASE, api } from './api'
import type {
  EquityPoint,
  KillSwitchState,
  Position,
  ResearchResult,
  ResearchScheduleState,
  ResearchStatus,
  Signal,
  SystemEvent,
  Trade,
} from './api'
import { EquityChart } from './components/EquityChart'
import { EventsFeed } from './components/EventsFeed'
import { KillSwitchPanel } from './components/KillSwitchPanel'
import { PositionsTable } from './components/PositionsTable'
import { ResearchPanel } from './components/ResearchPanel'
import { SignalsTable } from './components/SignalsTable'
import { StatTiles } from './components/StatTiles'
import { TradesTable } from './components/TradesTable'

const POLL_INTERVAL_MS = 15_000
const RESEARCH_POLL_INTERVAL_MS = 2_000

function App() {
  const [connected, setConnected] = useState(false)
  const [positions, setPositions] = useState<Position[]>([])
  const [equity, setEquity] = useState<EquityPoint[]>([])
  const [trades, setTrades] = useState<Trade[]>([])
  const [signals, setSignals] = useState<Signal[]>([])
  const [events, setEvents] = useState<SystemEvent[]>([])
  const [killSwitch, setKillSwitch] = useState<KillSwitchState | null>(null)
  const [research, setResearch] = useState<ResearchResult[]>([])
  const [researchSchedule, setResearchSchedule] = useState<ResearchScheduleState | null>(null)
  const [researchStatus, setResearchStatus] = useState<ResearchStatus | null>(null)

  const refresh = useCallback(async () => {
    try {
      await api.health()
      setConnected(true)
      const [positionsData, equityData, tradesData, signalsData, eventsData, killSwitchData, researchData, researchScheduleData, researchStatusData] =
        await Promise.all([
          api.positions(),
          api.equity(),
          api.trades(),
          api.signals(),
          api.events(),
          api.killSwitch(),
          api.research(),
          api.researchSchedule(),
          api.researchStatus(),
        ])
      setPositions(positionsData)
      setEquity(equityData)
      setTrades(tradesData)
      setSignals(signalsData)
      setEvents(eventsData)
      setKillSwitch(killSwitchData)
      setResearch(researchData)
      setResearchSchedule(researchScheduleData)
      setResearchStatus(researchStatusData)
    } catch {
      setConnected(false)
    }
  }, [])

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

  // While a research run is in flight, poll just its two endpoints much faster than the
  // normal 15s cycle, so the table/status visibly update within a couple seconds of the
  // run finishing instead of waiting for the next full poll.
  useEffect(() => {
    if (!researchStatus?.running) return
    const interval = setInterval(refreshResearch, RESEARCH_POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [researchStatus?.running, refreshResearch])

  useEffect(() => {
    const ws = new WebSocket(API_BASE.replace(/^http/, 'ws') + '/ws')
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)
      if (data.type === 'equity') {
        setEquity((prev) => {
          if (prev.length > 0 && prev[prev.length - 1].timestamp === data.timestamp) return prev
          return [...prev, { timestamp: data.timestamp, equity: data.equity, cash: prev[prev.length - 1]?.cash ?? 0 }]
        })
      }
    }
    return () => ws.close()
  }, [])

  return (
    <main className="dashboard">
      <div className="dashboard-header">
        <h1>Autotrader Dashboard</h1>
        <span className="connection-status">
          <span className={`connection-dot ${connected ? '' : 'offline'}`} />
          {connected ? 'Connected' : 'Backend unreachable'}
        </span>
      </div>

      <StatTiles equity={equity} positions={positions} />

      <div className="card">
        <h2>Equity</h2>
        <EquityChart data={equity} />
      </div>

      <div className="card">
        <h2>Kill switch</h2>
        <KillSwitchPanel state={killSwitch} onChange={setKillSwitch} />
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
        <h2>Positions</h2>
        <PositionsTable positions={positions} />
      </div>

      <div className="grid-2">
        <div className="card">
          <h2>Trade log</h2>
          <TradesTable trades={trades} />
        </div>
        <div className="card">
          <h2>Signal history</h2>
          <SignalsTable signals={signals} />
        </div>
      </div>

      <div className="card">
        <h2>System events</h2>
        <EventsFeed events={events} />
      </div>
    </main>
  )
}

export default App
