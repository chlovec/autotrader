export const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

export interface Position {
  symbol: string
  qty: number
  avg_entry_price: number
  market_value: number
  unrealized_pl: number
}

export interface EquityPoint {
  timestamp: string
  equity: number
  cash: number
}

export interface Trade {
  id: number
  broker_order_id: string
  symbol: string
  side: 'buy' | 'sell'
  qty: number
  fill_price: number | null
  status: string
  submitted_at: string
}

export interface Signal {
  id: number
  symbol: string
  strategy_name: string
  action: 'buy' | 'sell' | 'hold'
  reason: string
  timestamp: string
}

export interface SystemEvent {
  id: number
  level: 'info' | 'warning' | 'error' | 'critical'
  source: string
  message: string
  timestamp: string
}

export interface KillSwitchState {
  engaged: boolean
  reason: string
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`)
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`)
  return res.json()
}

export const api = {
  health: () => getJSON<{ status: string }>('/health'),
  positions: () => getJSON<Position[]>('/positions'),
  equity: () => getJSON<EquityPoint[]>('/equity'),
  trades: () => getJSON<Trade[]>('/trades'),
  signals: () => getJSON<Signal[]>('/signals'),
  events: () => getJSON<SystemEvent[]>('/events'),
  killSwitch: () => getJSON<KillSwitchState>('/kill-switch'),
  setKillSwitch: async (engaged: boolean, reason: string): Promise<KillSwitchState> => {
    const res = await fetch(`${API_BASE}/kill-switch?engaged=${engaged}&reason=${encodeURIComponent(reason)}`, { method: 'POST' })
    if (!res.ok) throw new Error(`kill-switch update failed: ${res.status}`)
    return res.json()
  },
}
