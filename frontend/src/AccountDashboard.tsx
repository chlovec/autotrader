import { useCallback, useEffect, useState } from 'react'
import type { AccountDetail, EquityPoint, KillSwitchState, Position, Signal, Trade } from './api'
import { accountWsUrl, api } from './api'
import { EquityChart } from './components/EquityChart'
import { KillSwitchPanel } from './components/KillSwitchPanel'
import { PositionsTable } from './components/PositionsTable'
import { SignalsTable } from './components/SignalsTable'
import { StatTiles } from './components/StatTiles'
import { TradesTable } from './components/TradesTable'
import { TradingLimitsPanel } from './components/TradingLimitsPanel'
import { navigate } from './router'

const POLL_INTERVAL_MS = 15_000

export function AccountDashboard({ accountId }: { accountId: string }) {
  const [account, setAccount] = useState<AccountDetail | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [positions, setPositions] = useState<Position[]>([])
  const [equity, setEquity] = useState<EquityPoint[]>([])
  const [trades, setTrades] = useState<Trade[]>([])
  const [signals, setSignals] = useState<Signal[]>([])
  const [killSwitch, setKillSwitch] = useState<KillSwitchState | null>(null)

  const refresh = useCallback(async () => {
    try {
      const accountData = await api.account(accountId)
      setAccount(accountData)
      setNotFound(false)
    } catch {
      setNotFound(true)
      return
    }

    const [equityData, tradesData, signalsData, killSwitchData] = await Promise.all([
      api.accountEquity(accountId),
      api.accountTrades(accountId),
      api.accountSignals(accountId),
      api.accountKillSwitch(accountId),
    ])
    setEquity(equityData)
    setTrades(tradesData)
    setSignals(signalsData)
    setKillSwitch(killSwitchData)

    try {
      setPositions(await api.accountPositions(accountId))
    } catch {
      setPositions([])
    }
  }, [accountId])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [refresh])

  useEffect(() => {
    const ws = new WebSocket(accountWsUrl(accountId))
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)
      if (data.type === 'equity') {
        setEquity((prev) => {
          if (prev.length > 0 && prev[prev.length - 1].timestamp === data.timestamp) return prev
          return [...prev, { timestamp: data.timestamp, equity: data.equity, cash: data.cash }]
        })
      } else if (data.type === 'positions') {
        setPositions(data.positions)
      } else if (data.type === 'trades') {
        setTrades((prev) => {
          const byId = new Map(prev.map((t) => [t.id, t]))
          for (const t of data.trades as Trade[]) byId.set(t.id, t)
          return [...byId.values()].sort((a, b) => a.submitted_at.localeCompare(b.submitted_at))
        })
      } else if (data.type === 'snapshot') {
        if (data.equity) {
          setEquity((prev) => {
            if (prev.length > 0 && prev[prev.length - 1].timestamp === data.equity.timestamp) return prev
            return [...prev, data.equity]
          })
        }
        setPositions(data.positions)
        setTrades(data.trades)
      }
    }
    return () => ws.close()
  }, [accountId])

  if (notFound) {
    return (
      <main className="dashboard">
        <p className="empty-note">Unknown account "{accountId}".</p>
        <button type="button" className="link-button" onClick={() => navigate('/')}>
          ← Back to accounts
        </button>
      </main>
    )
  }

  if (!account) {
    return (
      <main className="dashboard">
        <p className="empty-note">Loading account…</p>
      </main>
    )
  }

  return (
    <main className="dashboard">
      <div className="dashboard-header">
        <div>
          <button type="button" className="link-button" onClick={() => navigate('/')}>
            ← All accounts
          </button>
          <h1>{account.display_name}</h1>
          <p className="account-subtitle">
            {account.broker} · {account.strategy_name} ·{' '}
            <span className={`badge ${account.active ? 'badge-selected' : 'badge-inactive'}`}>
              {account.active ? 'Active' : 'Inactive'}
            </span>
          </p>
        </div>
      </div>

      <StatTiles equity={equity} positions={positions} />

      <div className="card">
        <h2>Equity</h2>
        <EquityChart data={equity} />
      </div>

      <div className="grid-2">
        <div className="card">
          <h2>Kill switch</h2>
          <KillSwitchPanel accountId={account.id} state={killSwitch} onChange={setKillSwitch} />
        </div>
        <div className="card">
          <h2>Trading limits</h2>
          <TradingLimitsPanel account={account} onChange={setAccount} />
        </div>
      </div>

      <div className="card">
        <h2>Positions</h2>
        {account.active ? (
          <PositionsTable positions={positions} />
        ) : (
          <p className="empty-note">Account is inactive - no live position data.</p>
        )}
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
    </main>
  )
}
