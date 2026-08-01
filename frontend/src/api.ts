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

export interface ResearchResult {
  id: number
  run_at: string
  symbol: string
  technical_score: number
  news_score: number
  combined_score: number
  rationale: string
  selected: boolean
}

export interface ResearchScheduleState {
  enabled: boolean
}

export interface ResearchStatus {
  running: boolean
}

export type TriggerResearchResult = { status: 'started' } | { status: 'already-running' }

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
  research: () => getJSON<ResearchResult[]>('/research'),
  researchSchedule: () => getJSON<ResearchScheduleState>('/research/schedule'),
  setResearchSchedule: async (enabled: boolean): Promise<ResearchScheduleState> => {
    const res = await fetch(`${API_BASE}/research/schedule?enabled=${enabled}`, { method: 'POST' })
    if (!res.ok) throw new Error(`research schedule update failed: ${res.status}`)
    return res.json()
  },
  researchStatus: () => getJSON<ResearchStatus>('/research/status'),
  triggerResearch: async (): Promise<TriggerResearchResult> => {
    const res = await fetch(`${API_BASE}/research/run`, { method: 'POST' })
    if (res.status === 409) return { status: 'already-running' }
    if (!res.ok) throw new Error(`research trigger failed: ${res.status}`)
    return res.json()
  },
}
